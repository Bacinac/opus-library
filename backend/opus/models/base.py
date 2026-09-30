"""What both halves of the library stand on: the declarative base and the
tables that describe the installation rather than its contents."""

import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class LoopPass(Base):
    __tablename__ = "loop_passes"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    finished_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
