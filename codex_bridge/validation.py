"""Standalone copy of the Research Chat wire limits; no application imports."""

import base64
import io
import re
from typing import Literal
from uuid import UUID
import warnings

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator


BODY_LIMIT = 9 * 1024 * 1024
IMAGE_LIMIT = 1024 * 1024
SESSION_PATTERN = r"^[0-9a-f]{64}$"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Message(Input):
    role: Literal["user", "assistant"]
    text: StrictStr = Field(default="", max_length=16000)
    images: list[StrictStr] = Field(default_factory=list, max_length=3, repr=False)

    @model_validator(mode="after")
    def check(self):
        if (self.role == "assistant" and self.images) or (self.role == "user" and len(self.text) > 8000):
            raise ValueError("Invalid message")
        # Reject lone surrogates before JSON-RPC serialization.
        self.text.encode("utf-8")
        for url in self.images:
            prefix = "data:image/jpeg;base64,"
            if not url.startswith(prefix) or len(url) > len(prefix) + 4 * ((IMAGE_LIMIT + 2) // 3):
                raise ValueError("Invalid image")
            try:
                raw = base64.b64decode(url[len(prefix):], validate=True)
                if len(raw) > IMAGE_LIMIT:
                    raise ValueError()
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(raw)) as image:
                        if image.format != "JPEG" or not all(1 <= side <= 1280 for side in image.size):
                            raise ValueError()
                        image.verify()
                    with Image.open(io.BytesIO(raw)) as image:
                        image.load()
            except (ValueError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                raise ValueError("Invalid image") from None
        return self


class Chat(Input):
    request_id: UUID
    model: StrictStr = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
    messages: list[Message] = Field(min_length=1, max_length=20, repr=False)

    @model_validator(mode="before")
    @classmethod
    def budget(cls, value):
        messages = value.get("messages") if isinstance(value, dict) else None
        if isinstance(messages, list):
            if len(messages) > 20 or sum(len(m.get("images", [])) for m in messages
                                        if isinstance(m, dict) and isinstance(m.get("images", []), list)) > 6:
                raise ValueError("Conversation limit exceeded")
        return value

    @model_validator(mode="after")
    def check(self):
        last = self.messages[-1]
        if last.role != "user" or not (last.text.strip() or last.images):
            raise ValueError("Last message must be a nonempty user message")
        if sum(len(m.text) for m in self.messages) > 64000 or sum(len(m.images) for m in self.messages) > 6:
            raise ValueError("Conversation limit exceeded")
        return self


class Lease(Input):
    sessions: list[StrictStr] = Field(max_length=2)

    @model_validator(mode="after")
    def check(self):
        if len(set(self.sessions)) != len(self.sessions) or any(not re.fullmatch(SESSION_PATTERN, s) for s in self.sessions):
            raise ValueError("Invalid session identifiers")
        return self
