from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LFS_", env_file=".env", extra="ignore")
    data_dir: Path = Path("data")
    model_dir: Path = Path("models")
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_roots: str = ""
    frontend_dir: Path = Path(__file__).resolve().parent.parent / "frontend" / "dist"

    def prepare(self):
        self.data_dir = self.data_dir.resolve()
        self.model_dir = self.model_dir.resolve()
        for folder in (self.data_dir, self.model_dir, self.data_dir / "thumbnails", self.data_dir / "exports"):
            folder.mkdir(parents=True, exist_ok=True)

    @property
    def roots(self) -> list[Path]:
        # A local-only server may explicitly register folders under the user's home.
        return [Path(p).expanduser().resolve() for p in self.allowed_roots.split(";") if p] or [Path.home().resolve()]
