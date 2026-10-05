from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from opus.models.base import Base


class FileImport(Base):
    __tablename__ = "file_imports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    entries: Mapped[list] = mapped_column(JSONB)
    committed: Mapped[bool] = mapped_column(Boolean, default=False)
