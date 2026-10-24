from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SourceConfig(BaseModel):
    id: Literal["wall", "rights"]
    name: str = Field(min_length=1, max_length=100)
    qzone_id: str = Field(default="", pattern=r"^(?:[1-9][0-9]{4,14})?$")


class LocalConfig(BaseModel):
    login_qq: str = Field(default="", pattern=r"^(?:[1-9][0-9]{4,14})?$")
    sources: list[SourceConfig] = Field(default_factory=lambda: [
        SourceConfig(id="wall", name="来源一"), SourceConfig(id="rights", name="来源二")])

    @model_validator(mode="after")
    def two_sources(self):
        if len(self.sources) != 2 or {s.id for s in self.sources} != {"wall", "rights"}:
            raise ValueError("需要 wall 和 rights 两个独立来源")
        configured = [s.qzone_id for s in self.sources if s.qzone_id]
        if len(set(configured)) != len(configured):
            raise ValueError("两个来源不能使用同一个空间标识")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    admin_token: str = ""
    downstream_token: str = ""
    data_dir: Path = Path("data")
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    daily_budget_cny: float = Field(default=5, ge=0)
    input_cny_per_million: float = Field(default=2.4, gt=0)
    output_cny_per_million: float = Field(default=9.6, gt=0)
    model_request_reserve_cny: float = Field(default=0.3, gt=0)
    bridge_base_url: str = "http://127.0.0.1:5700"
    bridge_token: str = ""
    model_enabled: bool = True
    scheduler_enabled: bool = True
    schedule_hours: str = "0,4,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23"
    max_pages: int = Field(default=40, ge=1, le=200)
    recovery_minutes: int = Field(default=10, ge=1, le=120)
    media_allowed_hosts: str = "qpic.cn,gtimg.cn,qq.com,photo.store.qq.com"
    media_allow_fake_ip_dns: bool = False
    max_media_bytes: int = 12 * 1024 * 1024

    @model_validator(mode="after")
    def check_hours(self):
        hours = [int(x) for x in self.schedule_hours.split(",")]
        if not hours or len(set(hours)) != len(hours) or any(h < 0 or h > 23 for h in hours):
            raise ValueError("SCHEDULE_HOURS 必须为互不重复的 0–23 整数")
        return self

    @property
    def hours(self) -> list[int]:
        return sorted(int(x) for x in self.schedule_hours.split(","))

    @property
    def local_config(self) -> LocalConfig:
        path = self.data_dir / "local-config.json"
        if not path.exists():
            return LocalConfig()
        try:
            return LocalConfig.model_validate_json(path.read_text())
        except ValueError:
            raise ValueError("本机 data/local-config.json 格式不正确，请对照示例配置") from None
