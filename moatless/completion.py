import json
import logging
import os
import random
import string
from typing import Optional, Union, Any, List, Tuple

import instructor
import litellm
import openai
from anthropic import Anthropic, AnthropicBedrock, NOT_GIVEN
from anthropic.types import ToolUseBlock
from anthropic.types.tool_result_block_param import Content
from instructor import OpenAISchema
from instructor.exceptions import InstructorRetryException
from litellm import cost_per_token, NotFoundError
from litellm.types.utils import ModelResponse
from openai import AzureOpenAI, OpenAI, LengthFinishReasonError
from pydantic import BaseModel, Field, model_validator

from moatless.settings import ModelSettings, LLMResponseFormat

logger = logging.getLogger(__name__)


class Message(BaseModel):
    role: str
    content: Optional[str] = None


class ToolCall(BaseModel):
    name: str
    input: dict[str, Any]


class AssistantMessage(Message):
    role: str = "assistant"
    content: Optional[str] = None
    tool_call: Optional[ToolCall] = None


class UserMessage(Message):
    role: str = "user"
    content: str


class Usage(BaseModel):
    completion_cost: float = 0
    completion_tokens: int = 0
    prompt_tokens: int = 0

    @classmethod
    def from_completion_response(
        cls, completion_response: dict | BaseModel, model: str
    ) -> Union["Usage", None]:
        if isinstance(completion_response, BaseModel) and hasattr(
            completion_response, "usage"
        ):
            usage = completion_response.usage.model_dump()
        elif isinstance(completion_response, dict) and "usage" in completion_response:
            usage = completion_response["usage"]
        else:
            logger.warning(
                f"No usage info available in completion response: {completion_response}"
            )
            return None

        logger.debug(f"Usage: {json.dumps(usage, indent=2)}")

        prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens", 0)

        if usage.get("cache_creation_input_tokens"):
            prompt_tokens += usage["cache_creation_input_tokens"]

        completion_tokens = usage.get("completion_tokens") or usage.get(
            "output_tokens", 0
        )

        try:
            cost = litellm.completion_cost(
                completion_response=completion_response, model=model
            )
        except Exception:
            # If cost calculation fails, fall back to calculating it manually
            try:
                prompt_cost, completion_cost = cost_per_token(
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
                cost = prompt_cost + completion_cost
            except NotFoundError as e:
                logger.debug(
                    f"Failed to calculate cost for completion response: {completion_response}. Error: {e}"
                )
                cost = 0
            except Exception as e:
                logger.error(
                    f"Failed to calculate cost for completion response: {completion_response}. Error: {e}"
                )
                cost = 0

        return cls(
            completion_cost=cost,
            completion_tokens=completion_tokens,
            prompt_tokens=prompt_tokens,
        )

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            completion_cost=self.completion_cost + other.completion_cost,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
        )

    def __str__(self) -> str:
        return (
            f"Usage(cost: ${self.completion_cost:.4f}, "
            f"completion tokens: {self.completion_tokens}, "
            f"prompt tokens: {self.prompt_tokens})"
        )


class Completion(BaseModel):
    model: str
    input: list[dict] | None = None
    response: dict[str, Any] | None = None
    usage: Usage | None = None

    @classmethod
    def from_llm_completion(
        cls, input_messages: list[dict], completion_response: Any, model: str
    ) -> Optional["Completion"]:
        if isinstance(completion_response, BaseModel):
            response = completion_response.model_dump()
        elif isinstance(completion_response, dict):
            response = completion_response
        else:
            logger.error(
                f"Unexpected completion response type: {type(completion_response)}"
            )
            return None

        usage = Usage.from_completion_response(completion_response, model)

        return cls(
            model=model,
            input=input_messages,
            response=response,
            usage=usage,
        )


class CompletionModel(BaseModel):
    model: str = Field(..., description="The model to use for completion")
    temperature: float = Field(0.0, description="The temperature to use for completion")
    max_tokens: int = Field(
        2000, description="The maximum number of tokens to generate"
    )
    model_base_url: Optional[str] = Field(
        default=None, description="The base URL for the model API"
    )
    model_api_key: Optional[str] = Field(
        default=None, description="The API key for the model"
    )
    response_format: LLMResponseFormat = Field(
        LLMResponseFormat.TOOLS, description="The response format expected from the LLM"
    )
    stop_words: Optional[list[str]] = Field(
        default=None, description="The stop words to use for completion"
    )

    @classmethod
    def from_settings(cls, settings: ModelSettings):
        if not settings:
            raise ValueError(
                "Model settings must be provided or set in the default settings."
            )
        return cls(
            model=settings.model,
            temperature=settings.temperature,
            model_base_url=settings.base_url,
            model_api_key=settings.api_key,
            # response_format=settings.response_format
        )

    @model_validator(mode="after")
    def validate_response_format(self):
        if self.model.startswith("claude") or self.model.startswith("anthropic"):
            self.response_format = LLMResponseFormat.ANTHROPIC_TOOLS
        elif self.model in [
            "gpt-4o-2024-08-06",
            "gpt-4o-mini-2024-07-18",
            "gpt-4o-mini",
        ]:
            self.response_format = LLMResponseFormat.STRUCTURED_OUTPUT
        elif self.model == "deepseek/deepseek-chat":
            self.response_format = LLMResponseFormat.JSON
        else:
            try:
                support_function_calling = litellm.supports_function_calling(
                    model=self.model
                )
            except Exception as e:
                support_function_calling = False

            if not support_function_calling:
                logger.debug(
                    f"The model {self.model} doens't support function calling, set response format to JSON"
                )
                self.response_format = LLMResponseFormat.JSON

        return self

    def _litellm_tool_completion(
        self,
        messages: list[dict],
        actions: list[type[OpenAISchema]],
        is_retry: bool = False,
    ) -> Tuple[OpenAISchema, ModelResponse]:
        litellm.drop_params = True

        tools = []
        for action in actions:
            tools.append(openai.pydantic_function_tool(action))

        completion_response = litellm.completion(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            stop=self.stop_words,
            tools=tools,
            tool_choice="auto",
            messages=messages,
        )

        tool_args, tool_name, retry_message = None, None, None
        if (
            not completion_response.choices[0].message.tool_calls
            and completion_response.choices[0].message.content
        ):
            if "```json" in completion_response.choices[0].message.content:
                logger.info(
                    f"Found no tool call but JSON in completion response, will try to parse"
                )

                try:
                    action_request = self.action_type().from_response(
                        completion_response, mode=instructor.Mode.TOOLS
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to parse JSON as tool call, will try to parse as JSON "
                    )

                    try:
                        action_request = self.action_type().from_response(
                            completion_response, mode=instructor.Mode.JSON
                        )
                    except Exception as e:
                        logger.exception(
                            f"Failed to parse JSON as tool call from completion response: {completion_response.choices[0].message.content}"
                        )
                        raise e

                return action_request, completion_response
            elif completion_response.choices[0].message.content.startswith("{"):
                tool_args = json.loads(completion_response.choices[0].message.content)

            if tool_args:
                if "name" in tool_args:
                    tool_name = tool_args.get("name")

                if "parameters" in tool_args:
                    tool_args = tool_args["parameters"]

        elif completion_response.choices[0].message.tool_calls[0]:
            tool_call = completion_response.choices[0].message.tool_calls[0]
            tool_args = json.loads(tool_call.function.arguments)
            tool_name = tool_call.function.name

        if not tool_args:
            if is_retry:
                logger.error(
                    f"No tool call return on request with tools: {tools}.\n\nCompletion response: {completion_response}"
                )
                raise ValueError(f"No tool call in response from LLM.")

            logger.warning(
                f"No tool call return on request with tools: {tools}.\n\nCompletion response: {completion_response}. Will retry"
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": completion_response.choices[0].message.content,
                }
            )
            if not retry_message:
                retry_message = "You must response with a tool call."
            messages.append({"role": "user", "content": retry_message})
            return self._litellm_tool_completion(messages, is_retry=True)

        action_request = self.action_type().from_tool_call(
            tool_args=tool_args, tool_name=tool_name
        )
        return action_request, completion_response

    def create_text_completion(self, messages: List[Message], system_prompt: str):
        completion_messages = self._map_completion_messages(messages)

        if self.response_format != LLMResponseFormat.ANTHROPIC_TOOLS:
            completion_messages.insert(0, {"role": "system", "content": system_prompt})

        response, completion_response = self._litellm_text_completion(
            completion_messages
        )

        completion = Completion.from_llm_completion(
            input_messages=completion_messages,
            completion_response=completion_response,
            model=self.model,
        )

        return response, completion

    def create_completion(
        self,
        messages: List[Message],
        system_prompt: str,
        actions: List[type[OpenAISchema]] | None = None,
    ) -> Tuple[OpenAISchema, Completion]:
        completion_messages = self._map_completion_messages(messages)

        if self.response_format != LLMResponseFormat.ANTHROPIC_TOOLS:
            completion_messages.insert(0, {"role": "system", "content": system_prompt})

        try:
            if self.response_format == LLMResponseFormat.ANTHROPIC_TOOLS:
                action_request, completion_response = self._anthropic_completion(
                    completion_messages
                )
            elif not actions:
                action_request, completion_response = self._litellm_text_completion(
                    completion_messages
                )
            elif self.response_format == LLMResponseFormat.STRUCTURED_OUTPUT:
                action_request, completion_response = self._openai_completion(
                    completion_messages, actions
                )
            elif self.response_format == LLMResponseFormat.TOOLS:
                action_request, completion_response = self._litellm_tool_completion(
                    completion_messages, actions
                )
            else:
                action_request, completion_response = self._instructor_completion(
                    completion_messages, actions
                )
        except InstructorRetryException as e:
            logger.warning(
                f"Failed to get completion response from LLM. {e}\n\nCompletion: {e.last_completion}"
            )
            raise e
        except Exception as e:
            logger.warning(
                f"Failed to get completion response from LLM. {e}. Input messages:\n {json.dumps(completion_messages, indent=2)}"
            )
            raise e

        completion = Completion.from_llm_completion(
            input_messages=completion_messages,
            completion_response=completion_response,
            model=self.model,
        )

        return action_request, completion

    def input_messages(
        self, content: str, completion: Completion | None, feedback: str | None = None
    ):
        messages = []
        tool_call_id = None

        if completion:
            messages = completion.input

            response_message = completion.response["choices"][0]["message"]
            if response_message.get("tool_calls"):
                tool_call_id = response_message.get("tool_calls")[0]["id"]
                last_response = {
                    "role": response_message["role"],
                    "tool_calls": response_message["tool_calls"],
                }
            else:
                last_response = {
                    "role": response_message["role"],
                    "content": response_message["content"],
                }
            messages.append(last_response)

            if response_message.get("tool_calls"):
                tool_call_id = response_message.get("tool_calls")[0]["id"]

        if tool_call_id:
            new_message = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            }
        else:
            new_message = {
                "role": "user",
                "content": content,
            }

        if feedback:
            new_message["content"] += "\n\n" + feedback

        messages.append(new_message)
        return messages

    def _litellm_text_completion(
        self, messages: list[dict]
    ) -> Tuple[str, ModelResponse]:
        litellm.drop_params = True

        completion_response = litellm.completion(
            model=self.model,
            base_url=self.model_base_url,
            api_key=self.model_api_key,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            stop=self.stop_words,
            messages=messages,
        )
        return completion_response.choices[0].message.content, completion_response

    def _instructor_completion(
        self,
        messages: list[dict],
        actions: List[type[OpenAISchema]] | None = None,
        is_retry: bool = False,
    ) -> Tuple[OpenAISchema, ModelResponse]:
        if self.response_format == LLMResponseFormat.JSON:
            client = instructor.from_litellm(
                litellm.completion, mode=instructor.Mode.MD_JSON
            )
        else:
            client = instructor.from_litellm(
                litellm.completion, mode=instructor.Mode.TOOLS
            )

        if actions:

            class TakeAction(OpenAISchema):
                action: Union[tuple(actions)] = Field(...)

                class Config:
                    smart_union = True

            action_type = TakeAction
        else:
            action_type = None

        try:
            take_action, completion_response = (
                client.chat.completions.create_with_completion(
                    model=self.model,
                    base_url=self.model_base_url,
                    api_key=self.model_api_key,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    stop=self.stop_words,
                    messages=messages,
                    response_model=action_type,
                )
            )

            if take_action.action:
                return take_action.action, completion_response
            else:
                logger.warning(
                    f"No action returned in response: {take_action}. Completion response: {completion_response}"
                )
                raise ValueError(f"No action returned in response: {take_action}.")

        except InstructorRetryException as e:
            logger.error(json.dumps(e.messages, indent=2))
            raise e
        except Exception as e:
            logger.exception(f"Failed to get completion response from litellm: {e}")
            raise e

    def function_call_system_prompt(self):
        return """You are an AI language model tasked with transforming unstructured messages wrapped in the XML tag <message> into structured tool calls. Your guidelines are:

         * Do not change, omit, or add any information from the original message.
         * Focus solely on converting the existing information into the correct tool call format.
         * Extract all relevant details necessary for the tool call without altering their meaning.
         * Ignore planned steps in the tool call
         * Provide the reasoning in the scratch_pad field

        Your response should be the tool call generated from the provided unstructured message, adhering strictly to these instructions."""

    def function_call_prompt(self, llm_response: str):
        content = "<message>\n"
        content += llm_response
        content += "\n</message>"
        return content

    def _openai_completion(
        self,
        messages: list[dict],
        actions: List[type[OpenAISchema]] | None = None,
        response_format: type[OpenAISchema] | None = None,
        is_retry: bool = False,
    ):
        if os.getenv("AZURE_API_KEY"):
            client = AzureOpenAI(
                api_key=os.getenv("AZURE_API_KEY"),
                api_version="2024-07-01-preview",
                azure_endpoint=os.getenv("AZURE_API_BASE"),
            )
        else:
            client = OpenAI()

        tools = []
        if actions:
            for action in actions:
                tools.append(openai.pydantic_function_tool(action))

        try:
            if actions:
                completion_response = client.beta.chat.completions.parse(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    stop=self.stop_words,
                    messages=messages,
                    tool_choice="required",
                    tools=tools,
                    parallel_tool_calls=False,
                )
            else:
                completion_response = client.beta.chat.completions.parse(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    stop=self.stop_words,
                    messages=messages,
                    response_format=response_format,
                )
        except LengthFinishReasonError as e:
            logger.error(
                f"Failed to parse completion response. Completion: {e.completion.model_dump_json(indent=2)}"
            )
            from moatless.actions.reject import Reject

            # TODO: Raise exception instead?
            return Reject(
                rejection_reason=f"Failed to generate action: {e}"
            ), e.completion

        if not actions:
            response = completion_response.choices[0].message.parsed
            return response, completion_response

        elif not completion_response.choices[0].message.tool_calls:
            if is_retry:
                logger.error(
                    f"No tool call return on request with tools: {tools}.\n\nCompletion response: {completion_response}"
                )
                raise RuntimeError(f"No tool call in response from LLM.")

            logger.warning(
                f"No tool call return on request with tools: {tools}.\n\nCompletion response: {completion_response}. Will retry"
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": completion_response.choices[0].message.content,
                }
            )
            messages.append(
                {"role": "user", "content": "You must response with a tool call."}
            )
            return self._openai_completion(messages, actions, response_format, is_retry)
        else:
            tool_call = completion_response.choices[0].message.tool_calls[0]
            action_request = tool_call.function.parsed_arguments
            return action_request, completion_response

    def _anthropic_completion(
        self, messages: list[dict]
    ) -> Tuple[OpenAISchema, Message]:
        if self.model.startswith("anthropic"):
            anthropic_client = AnthropicBedrock()
        else:
            anthropic_client = Anthropic()

        if self.action_type:
            tools = []
            tool_choice = {"type": "any"}
            if hasattr(self.action_type, "available_actions"):
                for action in self.action_type.available_actions():
                    tools.append(action.anthropic_schema)
            else:
                tools.append(self.action_type.anthropic_schema)
        else:
            tools = NOT_GIVEN
            tool_choice = NOT_GIVEN

        completion_response = anthropic_client.beta.prompt_caching.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=[
                {
                    "text": self.system_prompt(),
                    "type": "text",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tool_choice=tool_choice,
            tools=tools,
            messages=messages,
        )

        try:
            if not self.action_type:
                action_request = Content(content=completion_response.content[0].text)
            elif hasattr(self.action_type, "available_actions"):
                action_request = None
                if hasattr(self.action_type, "available_actions"):
                    for block in completion_response.content:
                        if isinstance(block, ToolUseBlock):
                            action = None
                            for (
                                available_action
                            ) in self.action_type.available_actions():
                                if available_action.__name__ == block.name:
                                    action = available_action
                                    break

                            if not action:
                                raise ValueError(f"Unknown action {block.name}")

                            tool_action_request = action.model_validate(block.input)

                            action_request = self.action_type(
                                action=tool_action_request
                            )

                            # TODO: We only support one action at the moment
                            break
                        else:
                            logger.warning(f"Unexpected block {block}]")
            else:
                action_request = self.action_type.from_response(
                    completion_response, mode=instructor.Mode.ANTHROPIC_TOOLS
                )

        except Exception as e:
            logger.exception(
                f"Failed to parse action request from completion response. Completion: {completion_response}"
            )
            raise e

        return action_request, completion_response

    def _map_completion_messages(self, messages: list[Message]) -> list[dict]:
        tool_call_id = None
        completion_messages = []
        for message in messages:
            if message.role == "user":
                if tool_call_id and self.response_format in [
                    LLMResponseFormat.TOOLS,
                    LLMResponseFormat.STRUCTURED_OUTPUT,
                ]:
                    completion_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": message.content,
                        }
                    )
                elif tool_call_id and self.response_format in [
                    LLMResponseFormat.TOOLS,
                    LLMResponseFormat.STRUCTURED_OUTPUT,
                ]:
                    completion_messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "tool_use_id": tool_call_id,
                                    "content": message.content,
                                    "type": "tool_result",
                                }
                            ],
                        }
                    )
                else:
                    completion_messages.append(
                        {"role": "user", "content": message.content}
                    )
            elif message.role == "assistant":
                if message.tool_call:
                    tool_call_id = generate_call_id()
                    if self.response_format == LLMResponseFormat.ANTHROPIC_TOOLS:
                        completion_messages.append(
                            {
                                "role": "assistant",
                                "content": [
                                    {
                                        "id": tool_call_id,
                                        "input": message.tool_call.input,
                                        "type": "tool_use",
                                        "name": message.tool_call.name,
                                    }
                                ],
                            }
                        )
                    elif self.response_format in [
                        LLMResponseFormat.TOOLS,
                        LLMResponseFormat.STRUCTURED_OUTPUT,
                    ]:
                        completion_messages.append(
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": tool_call_id,
                                        "type": "function",
                                        "function": {
                                            "name": message.tool_call.name,
                                            "arguments": json.dumps(
                                                message.tool_call.input
                                            ),
                                        },
                                    }
                                ],
                            }
                        )
                    else:
                        json_content = json.dumps(message.tool_call.input, indent=2)

                        # TODO Only if self.model.startswith("deepseek"): ?
                        json_content = f"```json\n{json_content}\n```"

                        completion_messages.append(
                            {
                                "role": "assistant",
                                "content": json_content,
                            }
                        )

                else:
                    tool_call_id = None
                    completion_messages.append(
                        {"role": "assistant", "content": message.content}
                    )

        return completion_messages

    def _get_tool_call(self, completion_response) -> Tuple[str, dict]:
        if (
            not completion_response.choices[0].message.tool_calls
            and completion_response.choices[0].message.content
        ):
            if "```json" in completion_response.choices[0].message.content:
                content = completion_response.choices[0].message.content
                json_start = content.index("```json") + 7
                json_end = content.rindex("```")
                json_content = content[json_start:json_end].strip()
            elif completion_response.choices[0].message.content.startswith("{"):
                json_content = completion_response.choices[0].message.content
            else:
                return None, None

            tool_call = json.loads(json_content)
            return tool_call.get("name"), tool_call

        elif completion_response.choices[0].message.tool_calls:
            tool_call = completion_response.choices[0].message.tool_calls[0]
            tool_dict = json.loads(tool_call.function.arguments)
            return tool_call.function.name, tool_dict

        return None


def generate_call_id():
    prefix = "call_"
    chars = string.ascii_letters + string.digits
    length = 24

    random_chars = "".join(random.choices(chars, k=length))

    random_string = prefix + random_chars

    return random_string
