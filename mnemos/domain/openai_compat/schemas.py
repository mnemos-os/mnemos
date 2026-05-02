from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text", "image_url"]
    text: Optional[str] = None
    image_url: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_payload(self):
        if self.type == "text" and self.text is None:
            raise ValueError("text content block requires text")
        if self.type == "image_url" and self.image_url is None:
            raise ValueError("image_url content block requires image_url")
        return self


ChatContent = Union[str, List[ContentBlock]]


class ToolFunction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    strict: Optional[bool] = None


class Tool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["function"]
    function: ToolFunction


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: Optional[ChatContent] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    function_call: Optional[Dict[str, Any]] = Field(default=None, exclude=True)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Optional[str] = "auto"
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    stream: bool = False
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    response_format: Optional[Dict[str, Any]] = None
    stop: Optional[Union[str, List[str]]] = None
    n: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    user: Optional[str] = None
    mnemos_inject_memory: Optional[bool] = Field(default=None, exclude=True)


class ChatCompletionStreamRequest(ChatCompletionRequest):
    stream: bool = True


class ChatCompletionResponseMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: Optional[ChatContent] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    function_call: Optional[Dict[str, Any]] = None
    refusal: Optional[str] = None
    audio: Optional[Dict[str, Any]] = None
    annotations: Optional[List[Dict[str, Any]]] = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatCompletionResponseMessage
    finish_reason: str


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Dict[str, int]


class ChatCompletionDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


class ChatCompletionStreamChoice(BaseModel):
    index: int
    delta: ChatCompletionDelta
    finish_reason: Optional[str] = None


class ChatCompletionStreamResponse(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChatCompletionStreamChoice]


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    owned_by: str


class ModelsResponse(BaseModel):
    object: str = "list"
    data: List[ModelInfo]
