import asyncio
import hashlib
import logging
import os
from collections.abc import Callable
from enum import Enum
from typing import Literal

import anthropic
import cohere
import diskcache as dc  # type: ignore
import httpx
import openai
import tiktoken
import voyageai  # type: ignore
import voyageai.error  # type: ignore
from anthropic import NOT_GIVEN, Anthropic, AsyncAnthropic, NotGiven
from anthropic.types import MessageParam
from openai import AsyncOpenAI, RateLimitError
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, computed_field

from legalbenchrag.utils.credentials import credentials

logger = logging.getLogger("uvicorn")

# AI Types


class AIModel(BaseModel):
    company: Literal["openai", "anthropic"]
    model: str

    @computed_field  # type: ignore[misc]
    @property
    def ratelimit_tpm(self) -> float:
        match self.company:
            case "openai":
                # Tier 5
                match self.model:
                    case "gpt-4o-mini":
                        return 150000000
                    case "gpt-4o":
                        return 30000000
                    case m if m.startswith("gpt-4-turbo"):
                        return 2000000
                    case _:
                        return 1000000
            case "anthropic":
                # Tier 4
                return 400000


class AIMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class AIEmbeddingModel(BaseModel):
    company: Literal["openai", "cohere", "voyageai"]
    model: str

    @computed_field  # type: ignore[misc]
    @property
    def ratelimit_rpm(self) -> float:
        match self.company:
            case "openai":
                return 10000
            case "cohere":
                return 10000
            case "voyageai":
                # It says 300RPM but I can only get 30 out of it
                return 30


class AIEmbeddingType(Enum):
    DOCUMENT = 1
    QUERY = 2


class AIRerankModel(BaseModel):
    company: Literal["cohere", "voyageai"]
    model: str

    @computed_field  # type: ignore[misc]
    @property
    def ratelimit_rpm(self) -> float:
        match self.company:
            case "cohere":
                return 10000
            case "voyageai":
                # It says 100RPM but I can only get 60 out of it
                return 60


# Cache
os.makedirs("./data/cache", exist_ok=True)
cache = dc.Cache("./data/cache/ai_cache.db")

RATE_LIMIT_RATIO = 0.95


class AIConnection:
    openai_client: AsyncOpenAI
    voyageai_client: voyageai.AsyncClient
    cohere_client: cohere.AsyncClient
    anthropic_client: AsyncAnthropic
    sync_anthropic_client: Anthropic
    # Share one global Semaphore across all threads
    cohere_ratelimit_semaphore = asyncio.Semaphore(1)
    voyageai_ratelimit_semaphore = asyncio.Semaphore(1)
    openai_ratelimit_semaphore = asyncio.Semaphore(1)
    anthropic_ratelimit_semaphore = asyncio.Semaphore(1)

    def __init__(self) -> None:
        self.openai_client = AsyncOpenAI(
            api_key=credentials.ai.openai_api_key.get_secret_value()
        )
        self.anthropic_client = AsyncAnthropic(
            api_key=credentials.ai.anthropic_api_key.get_secret_value()
        )
        self.sync_anthropic_client = Anthropic(
            api_key=credentials.ai.anthropic_api_key.get_secret_value()
        )
        self.voyageai_client = voyageai.AsyncClient(
            api_key=credentials.ai.voyageai_api_key.get_secret_value()
        )
        self.cohere_client = cohere.AsyncClient(
            api_key=credentials.ai.cohere_api_key.get_secret_value()
        )


# NOTE: API Clients cannot be called from multiple event loops,
# So every asyncio event loop needs its own API connection
ai_connections: dict[asyncio.AbstractEventLoop, AIConnection] = {}


def get_ai_connection() -> AIConnection:
    event_loop = asyncio.get_event_loop()
    if event_loop not in ai_connections:
        ai_connections[event_loop] = AIConnection()
    return ai_connections[event_loop]


class AIError(Exception):
    """A class for AI Task Errors"""


class AIValueError(AIError, ValueError):
    """A class for AI Value Errors"""


class AITimeoutError(AIError, TimeoutError):
    """A class for AI Task Timeout Errors"""


def ai_num_tokens(model: AIModel, s: str) -> int:
    if model.company == "anthropic":
        # Doesn't actually connect to the network
        return get_ai_connection().sync_anthropic_client.count_tokens(s)
    elif model.company == "openai":
        encoding = tiktoken.encoding_for_model(model.model)
        num_tokens = len(encoding.encode(s))
        return num_tokens


def get_call_cache_key(
    model: AIModel,
    messages: list[AIMessage],
) -> str:
    # Hash the array of texts
    md5_hasher = hashlib.md5()
    md5_hasher.update(model.model_dump_json().encode())
    for message in messages:
        md5_hasher.update(md5_hasher.hexdigest().encode())
        md5_hasher.update(message.model_dump_json().encode())
    key = md5_hasher.hexdigest()

    return key


async def ai_call(
    model: AIModel,
    messages: list[AIMessage],
    *,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    # When using anthropic, the first message must be from the user.
    # If the first message is not a User, this message will be prepended to the messages.
    anthropic_initial_message: str | None = "<START>",
    # If two messages of the same role are given to anthropic, they must be concatenated.
    # This is the delimiter between concatenated.
    anthropic_combine_delimiter: str = "\n",
    # Throw an AITimeoutError after this many retries fail
    num_ratelimit_retries: int = 10,
    # Backoff function (Receives index of attempt)
    backoff_algo: Callable[[int], float] = lambda i: min(2**i, 5),
) -> str:
    cache_key = get_call_cache_key(model, messages)
    cached_call = cache.get(cache_key)

    if cached_call is not None:
        return cached_call

    num_tokens_input: int = sum(
        [ai_num_tokens(model, message.content) for message in messages]
    )

    return_value: str | None = None
    match model.company:
        case "openai":
            for i in range(num_ratelimit_retries):
                try:
                    # Guard with ratelimit
                    async with get_ai_connection().openai_ratelimit_semaphore:
                        tpm = model.ratelimit_tpm
                        expected_wait = num_tokens_input / (tpm * RATE_LIMIT_RATIO / 60)
                        await asyncio.sleep(expected_wait)

                    def ai_message_to_openai_message_param(
                        message: AIMessage,
                    ) -> ChatCompletionMessageParam:
                        if message.role == "system":  # noqa: SIM114
                            return {"role": message.role, "content": message.content}
                        elif message.role == "user":  # noqa: SIM114
                            return {"role": message.role, "content": message.content}
                        elif message.role == "assistant":
                            return {"role": message.role, "content": message.content}

                    if i > 0:
                        logger.debug("Trying again after RateLimitError...")
                    response = (
                        await get_ai_connection().openai_client.chat.completions.create(
                            model=model.model,
                            messages=[
                                ai_message_to_openai_message_param(message)
                                for message in messages
                            ],
                            temperature=temperature,
                            max_tokens=max_tokens,
                        )
                    )
                    assert response.choices[0].message.content is not None
                    return_value = response.choices[0].message.content
                    break
                except RateLimitError:
                    logger.warning("OpenAI RateLimitError")
                    async with get_ai_connection().openai_ratelimit_semaphore:
                        await asyncio.sleep(backoff_algo(i))
            if return_value is None:
                raise AITimeoutError("Cannot overcome OpenAI RateLimitError")

        case "anthropic":
            for i in range(num_ratelimit_retries):
                try:
                    # Guard with ratelimit
                    async with get_ai_connection().anthropic_ratelimit_semaphore:
                        tpm = model.ratelimit_tpm
                        expected_wait = num_tokens_input / (tpm * RATE_LIMIT_RATIO / 60)
                        await asyncio.sleep(expected_wait)

                    def ai_message_to_anthropic_message_param(
                        message: AIMessage,
                    ) -> MessageParam:
                        if message.role == "user" or message.role == "assistant":
                            return {"role": message.role, "content": message.content}
                        elif message.role == "system":
                            raise AIValueError(
                                "system not allowed in anthropic message param"
                            )

                    if i > 0:
                        logger.debug("Trying again after RateLimitError...")

                    # Extract system message if it exists
                    system: str | NotGiven = NOT_GIVEN
                    if len(messages) > 0 and messages[0].role == "system":
                        system = messages[0].content
                        messages = messages[1:]
                    # Insert initial message if necessary
                    if (
                        anthropic_initial_message is not None
                        and len(messages) > 0
                        and messages[0].role != "user"
                    ):
                        messages = [
                            AIMessage(role="user", content=anthropic_initial_message)
                        ] + messages
                    # Combined messages (By combining consecutive messages of the same role)
                    combined_messages: list[AIMessage] = []
                    for message in messages:
                        if (
                            len(combined_messages) == 0
                            or combined_messages[-1].role != message.role
                        ):
                            combined_messages.append(message)
                        else:
                            # Copy before edit
                            combined_messages[-1] = combined_messages[-1].model_copy(
                                deep=True
                            )
                            # Merge consecutive messages with the same role
                            combined_messages[-1].content += (
                                anthropic_combine_delimiter + message.content
                            )
                    # Get the response
                    response_message = (
                        await get_ai_connection().anthropic_client.messages.create(
                            model=model.model,
                            system=system,
                            messages=[
                                ai_message_to_anthropic_message_param(message)
                                for message in combined_messages
                            ],
                            temperature=0.0,
                            max_tokens=max_tokens,
                        )
                    )
                    assert isinstance(
                        response_message.content[0], anthropic.types.TextBlock
                    )
                    assert isinstance(response_message.content[0].text, str)
                    return_value = response_message.content[0].text
                    break
                except anthropic.RateLimitError as e:
                    logger.warning(f"Anthropic Error: {repr(e)}")
                    async with get_ai_connection().anthropic_ratelimit_semaphore:
                        await asyncio.sleep(backoff_algo(i))
            if return_value is None:
                raise AITimeoutError("Cannot overcome Anthropic RateLimitError")

    cache.set(cache_key, return_value)
    return return_value


def get_embeddings_cache_key(
    model: AIEmbeddingModel, text: str, embedding_type: AIEmbeddingType
) -> str:
    key = f"{model.company}||||{model.model}||||{embedding_type.name}||||{hashlib.md5(text.encode()).hexdigest()}"
    return key


async def ai_embedding(
    model: AIEmbeddingModel,
    text: str,
    embedding_type: AIEmbeddingType,
    *,
    # Throw an AITimeoutError after this many retries fail
    num_ratelimit_retries: int = 10,
    # Backoff function (Receives index of attempt)
    backoff_algo: Callable[[int], float] = lambda i: min(2**i, 5),
) -> list[float]:
    cache_key = get_embeddings_cache_key(model, text, embedding_type)
    cached_embedding = cache.get(cache_key)

    if cached_embedding is not None:
        return cached_embedding

    embedding: list[float] | None = None
    match model.company:
        case "openai":
            for i in range(num_ratelimit_retries):
                try:
                    async with get_ai_connection().openai_ratelimit_semaphore:
                        rpm = model.ratelimit_rpm * RATE_LIMIT_RATIO
                        await asyncio.sleep(60.0 / rpm)
                    response = (
                        await get_ai_connection().openai_client.embeddings.create(
                            input=[text],
                            model=model.model,
                        )
                    )
                    embedding = response.data[0].embedding
                    break
                except openai.RateLimitError:
                    logger.warning("OpenAI RateLimitError")
                    async with get_ai_connection().openai_ratelimit_semaphore:
                        await asyncio.sleep(backoff_algo(i))
            if embedding is None:
                raise AITimeoutError("Cannot overcome OpenAI RateLimitError")
        case "cohere":
            for i in range(num_ratelimit_retries):
                try:
                    async with get_ai_connection().cohere_ratelimit_semaphore:
                        rpm = model.ratelimit_rpm * RATE_LIMIT_RATIO
                        await asyncio.sleep(60.0 / rpm)
                    result = await get_ai_connection().cohere_client.embed(
                        texts=[text],
                        model=model.model,
                        input_type=(
                            "search_document"
                            if embedding_type == AIEmbeddingType.DOCUMENT
                            else "search_query"
                        ),
                    )
                    assert isinstance(result.embeddings, list)
                    embedding = result.embeddings[0]
                    break
                except voyageai.error.RateLimitError:
                    logger.warning("Cohere RateLimitError")
                    async with get_ai_connection().cohere_ratelimit_semaphore:
                        await asyncio.sleep(backoff_algo(i))
            if embedding is None:
                raise AITimeoutError("Cannot overcome Cohere RateLimitError")
        case "voyageai":
            for i in range(num_ratelimit_retries):
                try:
                    async with get_ai_connection().voyageai_ratelimit_semaphore:
                        rpm = model.ratelimit_rpm * RATE_LIMIT_RATIO
                        await asyncio.sleep(60.0 / rpm)
                    result = await get_ai_connection().voyageai_client.embed(
                        [text],
                        model=model.model,
                        input_type=(
                            "document"
                            if embedding_type == AIEmbeddingType.DOCUMENT
                            else "query"
                        ),
                    )
                    assert isinstance(result.embeddings, list)
                    embedding = result.embeddings[0]
                    break
                except voyageai.error.RateLimitError:
                    logger.warning("VoyageAI RateLimitError")
                    async with get_ai_connection().voyageai_ratelimit_semaphore:
                        await asyncio.sleep(backoff_algo(i))
            if embedding is None:
                raise AITimeoutError("Cannot overcome VoyageAI RateLimitError")
    cache.set(cache_key, embedding)
    return embedding


def get_rerank_cache_key(
    model: AIRerankModel, query: str, texts: list[str], top_k: int | None
) -> str:
    # Hash the array of texts
    md5_hasher = hashlib.md5()
    md5_hasher.update(query.encode())
    for text in texts:
        md5_hasher.update(md5_hasher.hexdigest().encode())
        md5_hasher.update(text.encode())
    texts_hash = md5_hasher.hexdigest()

    key = f"{model.company}||||{model.model}||||{top_k}||||{texts_hash}"
    return key


# Gets the list of indices that reranks the original texts
async def ai_rerank(
    model: AIRerankModel,
    query: str,
    texts: list[str],
    *,
    top_k: int | None = None,
    # Throw an AITimeoutError after this many retries fail
    num_ratelimit_retries: int = 10,
    # Backoff function (Receives index of attempt)
    backoff_algo: Callable[[int], float] = lambda i: min(2**i, 5),
) -> list[int]:
    cache_key = get_rerank_cache_key(model, query, texts, top_k)
    cached_reranking = cache.get(cache_key)

    if cached_reranking is not None:
        return cached_reranking

    indices: list[int] | None = None
    match model.company:
        case "cohere":
            for i in range(num_ratelimit_retries):
                try:
                    async with get_ai_connection().cohere_ratelimit_semaphore:
                        rpm = model.ratelimit_rpm * RATE_LIMIT_RATIO
                        await asyncio.sleep(60.0 / rpm)
                    response = await get_ai_connection().cohere_client.rerank(
                        model=model.model,
                        query=query,
                        documents=texts,
                        top_n=top_k,
                    )
                    indices = [result.index for result in response.results]
                    break
                except (cohere.errors.TooManyRequestsError, httpx.ConnectError):
                    logger.warning("Cohere RateLimitError")
                    async with get_ai_connection().cohere_ratelimit_semaphore:
                        await asyncio.sleep(backoff_algo(i))
            if indices is None:
                raise AITimeoutError("Cannot overcome Cohere RateLimitError")
        case "voyageai":
            for i in range(num_ratelimit_retries):
                try:
                    async with get_ai_connection().voyageai_ratelimit_semaphore:
                        rpm = model.ratelimit_rpm * RATE_LIMIT_RATIO
                        await asyncio.sleep(60.0 / rpm)
                    voyageai_response = (
                        await get_ai_connection().voyageai_client.rerank(
                            query=query,
                            documents=texts,
                            model=model.model,
                            top_k=top_k,
                        )
                    )
                    indices = [
                        int(result.index) for result in voyageai_response.results
                    ]
                    break
                except voyageai.error.RateLimitError:
                    logger.warning("VoyageAI RateLimitError")
                    async with get_ai_connection().voyageai_ratelimit_semaphore:
                        await asyncio.sleep(backoff_algo(i))
            if indices is None:
                raise AITimeoutError("Cannot overcome VoyageAI RateLimitError")
    cache.set(cache_key, indices)
    return indices
