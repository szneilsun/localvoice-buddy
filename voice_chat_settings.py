import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


COMPLETIONS_PATH = "/chat/completions"


def completion_url(value):
    if not isinstance(value, str) or not re.fullmatch(r"[!-~]{1,2048}", value):
        raise ValueError("请输入有效的 HTTPS API URL。")
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.port == 0 or any(char in value for char in "\\?#")
                or not re.fullmatch(r"[a-zA-Z0-9.:\[\]-]+", url.netloc)):
            raise ValueError
    except ValueError:
        raise ValueError("API URL 必须使用 HTTPS，且不能包含凭据、查询参数或片段。") from None
    path = url.path.rstrip("/") or "/v1"
    if not path.endswith(COMPLETIONS_PATH):
        path += COMPLETIONS_PATH
    return urlunsplit(("https", url.netloc.lower(), path, "", ""))


def api_base_url(value):
    endpoint = completion_url(value)
    if urlsplit(endpoint).path == COMPLETIONS_PATH:
        # A bare host implies /v1, so keep an explicit root-level endpoint intact.
        return endpoint
    return endpoint.removesuffix(COMPLETIONS_PATH)


@dataclass(frozen=True, slots=True)
class OpenAIConfig:
    url: str
    model: str
    api_key: str = field(repr=False)

    @property
    def endpoint(self):
        return completion_url(self.url)


@dataclass(frozen=True, slots=True)
class ChatSettings:
    provider: str = "local"
    remote: OpenAIConfig | None = None

    def public(self):
        return {
            "provider": self.provider,
            "url": self.remote.url if self.remote else "",
            "model": self.remote.model if self.remote else "",
            "has_api_key": self.remote is not None,
        }

    def update(self, data):
        if not isinstance(data, dict) or data.get("provider") not in ("local", "remote"):
            raise ValueError("请选择本地或远程模型。")
        if data["provider"] == "local":
            return ChatSettings(remote=None if data.get("clear_remote") is True else self.remote)
        url = api_base_url(data.get("url"))
        model = data.get("model")
        key = data.get("api_key", "")
        if not isinstance(model, str) or not re.fullmatch(r"[!-~]{1,256}", model):
            raise ValueError("请输入 API 提供商给出的模型名称。")
        if key == "" and self.remote and url == self.remote.url:
            key = self.remote.api_key
        if not isinstance(key, str) or not re.fullmatch(r"[!-~]{1,4096}", key):
            raise ValueError("请输入 API Key。更换 API URL 时需要重新输入。")
        return ChatSettings("remote", OpenAIConfig(url, model, key))

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("Invalid chat settings.")
        if data.get("provider") == "remote":
            return cls().update(data)
        remote = cls().update(dict(data, provider="remote")).remote if "api_key" in data else None
        return cls(remote=remote).update(data)

    def save(self, path):
        data = {"provider": self.provider}
        if self.remote:
            data.update(url=self.remote.url, model=self.remote.model, api_key=self.remote.api_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".chat-settings-", delete=False) as file:
                temp = file.name
                json.dump(data, file)
            os.replace(temp, path)
        finally:
            if temp is not None and os.path.exists(temp):
                os.unlink(temp)
