import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(ValueError):
    """Configuration that would stop the app from running correctly."""


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LFS_", env_file=".env", extra="ignore", protected_namespaces=())
    data_dir: Path = Path("data")
    model_dir: Path = Path("models")
    host: str = "0.0.0.0"
    port: int = Field(8765, ge=1, le=65535)
    allowed_roots: str = ""
    frontend_dir: Path = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    # Optional ONNX models (scripts/fetch_models.py): unload after this many idle seconds.
    model_idle_seconds: float = Field(300.0, ge=5, le=86400)
    dino_variant: Literal["small", "base"] = "small"
    # Watch library folders and index new files within seconds.
    watch: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value):
        return str(value).upper()

    def validate_runtime(self) -> list[str]:
        """Check the environment after prepare(). Raises ConfigError for fatal problems,
        returns warnings for things that degrade features."""
        warnings: list[str] = []
        probe = self.data_dir / ".write-test"
        try:
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise ConfigError(f"LFS_DATA_DIR {self.data_dir} is not writable: {exc}") from exc
        free = shutil.disk_usage(self.data_dir).free
        if free < 1_000_000_000:
            warnings.append(f"Only {free / 1e9:.1f} GB free under {self.data_dir}; indexing and backups may fail.")
        for root in self.roots:
            if not root.is_dir():
                warnings.append(f"Allowed root {root} does not exist.")
        if not (self.model_dir / "buffalo_l").is_dir():
            warnings.append(f"Face models not found in {self.model_dir / 'buffalo_l'}; face indexing is disabled.")
        if self.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("LFS_ALLOW_LAN"):
            warnings.append(f"Listening on {self.host}: other devices on your network can reach the app. "
                            "Set LFS_HOST=127.0.0.1 to keep it on this machine.")
        return warnings

    def prepare(self):
        self.data_dir = self.data_dir.resolve()
        self.model_dir = self.model_dir.resolve()
        for folder in (
            self.data_dir,
            self.model_dir,
            self.data_dir / "thumbnails",
            self.data_dir / "exports",
            self.data_dir / "video_cache",
        ):
            folder.mkdir(parents=True, exist_ok=True)

    @property
    def roots(self) -> list[Path]:
        # A local-only server may explicitly register folders under the user's home.
        return [Path(p).expanduser().resolve() for p in self.allowed_roots.split(";") if p] or [Path.home().resolve()]
