from opus_core.db import connect, session_dependency

from opus.config import settings

engine, SessionLocal = connect(settings.database_url)
get_session = session_dependency(SessionLocal)
