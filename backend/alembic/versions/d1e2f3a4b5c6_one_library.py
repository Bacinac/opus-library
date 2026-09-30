"""one library: the video half moves in beside the music half

Two things happen here, and they are the same event. The music tables that had
generic names give them up — `files` and `downloads` were only unambiguous while
this database held one kind of media — and the video half's tables are created
alongside them under names that say which half they belong to.

The settings that existed once per app and now exist once per install are
renamed the same way: a quality profile means something different to an album
than to a film, so neither of them gets to be simply `quality_profile`.

Revision ID: d1e2f3a4b5c6
Revises: c156f50f84c6
Create Date: 2026-08-15

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'd1e2f3a4b5c6'
down_revision = 'c156f50f84c6'
branch_labels = None
depends_on = None

_SETTING_RENAMES = (
    ('quality_profile', 'music_quality_profile'),
    ('naming_template', 'music_naming'),
)


# Postgres renames a table but nothing that hangs off it, so the indexes,
# constraints and the enum type would all keep saying `files` and `downloads`
# forever. A name that lies is worse than a long one.
_RENAMED_INDEXES = (
    ('files_pkey', 'music_files_pkey'),
    ('files_path_key', 'music_files_path_key'),
    ('ix_files_track_id', 'ix_music_files_track_id'),
    ('ix_files_download_id', 'ix_music_files_download_id'),
    ('downloads_pkey', 'music_downloads_pkey'),
)
_RENAMED_CONSTRAINTS = (
    ('music_files', 'files_track_id_fkey', 'music_files_track_id_fkey'),
    ('music_downloads', 'downloads_release_id_fkey', 'music_downloads_release_id_fkey'),
)


def upgrade():
    op.rename_table('files', 'music_files')
    op.rename_table('downloads', 'music_downloads')
    for old, new in _RENAMED_INDEXES:
        op.execute(f'ALTER INDEX {old} RENAME TO {new}')
    for table, old, new in _RENAMED_CONSTRAINTS:
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new}')
    op.execute('ALTER TYPE downloadstatus RENAME TO musicdownloadstatus')

    for old, new in _SETTING_RENAMES:
        op.execute(sa.text("UPDATE settings SET key = :new WHERE key = :old")
                   .bindparams(new=new, old=old))

    op.create_table(
        'movies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tmdb_id', sa.Integer(), nullable=False),
        sa.Column('imdb_id', sa.String(), nullable=True),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('original_title', sa.String(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=True),
        sa.Column('overview', sa.Text(), nullable=False),
        sa.Column('poster_url', sa.String(), nullable=True),
        sa.Column('backdrop_url', sa.String(), nullable=True),
        sa.Column('runtime_min', sa.Integer(), nullable=True),
        sa.Column('monitored', sa.Boolean(), nullable=False),
        sa.Column('subtitle_override', sa.String(), nullable=False),
        sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_movies_tmdb_id'), 'movies', ['tmdb_id'], unique=True)

    op.create_table(
        'series',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tmdb_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('original_title', sa.String(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=True),
        sa.Column('overview', sa.Text(), nullable=False),
        sa.Column('poster_url', sa.String(), nullable=True),
        sa.Column('backdrop_url', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('monitored', sa.Boolean(), nullable=False),
        sa.Column('subtitle_override', sa.String(), nullable=False),
        sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_series_tmdb_id'), 'series', ['tmdb_id'], unique=True)

    op.create_table(
        'web_channels',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('external_id', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('thumb_url', sa.String(), nullable=True),
        sa.Column('monitored', sa.Boolean(), nullable=False),
        sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('url'),
    )

    op.create_table(
        'seasons',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('series_id', sa.Integer(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('monitored', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['series_id'], ['series.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('series_id', 'number'),
    )
    op.create_index(op.f('ix_seasons_series_id'), 'seasons', ['series_id'], unique=False)

    op.create_table(
        'web_videos',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('channel_id', sa.Integer(), nullable=True),
        sa.Column('external_id', sa.String(), nullable=False),
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('uploader', sa.String(), nullable=False),
        sa.Column('thumb_url', sa.String(), nullable=True),
        sa.Column('duration_s', sa.Integer(), nullable=True),
        sa.Column('published_at', sa.Date(), nullable=True),
        sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['channel_id'], ['web_channels.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('external_id'),
    )
    op.create_index(op.f('ix_web_videos_channel_id'), 'web_videos', ['channel_id'], unique=False)

    op.create_table(
        'episodes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('season_id', sa.Integer(), nullable=False),
        sa.Column('tmdb_id', sa.Integer(), nullable=True),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('air_date', sa.Date(), nullable=True),
        sa.Column('overview', sa.Text(), nullable=False),
        sa.Column('monitored', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['season_id'], ['seasons.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('season_id', 'number'),
    )
    op.create_index(op.f('ix_episodes_season_id'), 'episodes', ['season_id'], unique=False)

    op.create_table(
        'video_files',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('path', sa.String(), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('container', sa.String(), nullable=False),
        sa.Column('video_codec', sa.String(), nullable=False),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('audio_langs', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('movie_id', sa.Integer(), nullable=True),
        sa.Column('episode_id', sa.Integer(), nullable=True),
        sa.Column('web_video_id', sa.Integer(), nullable=True),
        sa.Column('scanned_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['episode_id'], ['episodes.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['movie_id'], ['movies.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['web_video_id'], ['web_videos.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('path'),
    )
    op.create_index(op.f('ix_video_files_episode_id'), 'video_files', ['episode_id'], unique=False)
    op.create_index(op.f('ix_video_files_movie_id'), 'video_files', ['movie_id'], unique=False)
    op.create_index(op.f('ix_video_files_web_video_id'), 'video_files', ['web_video_id'],
                    unique=False)

    op.create_table(
        'subtitles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('file_id', sa.Integer(), nullable=False),
        sa.Column('lang', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('format', sa.String(), nullable=False),
        sa.Column('forced', sa.Boolean(), nullable=False),
        sa.Column('auto_generated', sa.Boolean(), nullable=False),
        sa.Column('path', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['file_id'], ['video_files.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_subtitles_file_id'), 'subtitles', ['file_id'], unique=False)

    op.create_table(
        'video_downloads',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('movie_id', sa.Integer(), nullable=True),
        sa.Column('episode_id', sa.Integer(), nullable=True),
        sa.Column('web_video_id', sa.Integer(), nullable=True),
        sa.Column('channel', sa.String(), nullable=False),
        sa.Column('release_title', sa.String(), nullable=False),
        sa.Column('job_ref', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('progress', sa.Float(), nullable=False),
        sa.Column('detail', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['episode_id'], ['episodes.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['movie_id'], ['movies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['web_video_id'], ['web_videos.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_video_downloads_episode_id'), 'video_downloads', ['episode_id'],
                    unique=False)
    op.create_index(op.f('ix_video_downloads_movie_id'), 'video_downloads', ['movie_id'],
                    unique=False)
    op.create_index(op.f('ix_video_downloads_state'), 'video_downloads', ['state'], unique=False)
    op.create_index(op.f('ix_video_downloads_web_video_id'), 'video_downloads', ['web_video_id'],
                    unique=False)


def downgrade():
    op.drop_table('video_downloads')
    op.drop_table('subtitles')
    op.drop_table('video_files')
    op.drop_table('episodes')
    op.drop_table('web_videos')
    op.drop_table('seasons')
    op.drop_table('web_channels')
    op.drop_table('series')
    op.drop_table('movies')

    for old, new in _SETTING_RENAMES:
        op.execute(sa.text("UPDATE settings SET key = :old WHERE key = :new")
                   .bindparams(new=new, old=old))

    op.execute('ALTER TYPE musicdownloadstatus RENAME TO downloadstatus')
    for table, old, new in _RENAMED_CONSTRAINTS:
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT {new} TO {old}')
    for old, new in _RENAMED_INDEXES:
        op.execute(f'ALTER INDEX {new} RENAME TO {old}')
    op.rename_table('music_downloads', 'downloads')
    op.rename_table('music_files', 'files')
