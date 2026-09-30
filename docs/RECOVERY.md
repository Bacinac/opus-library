# OPUS · Library recovery

This runbook protects the database and the files that make its private vault
meaningful. It is for an operator who already has SSH access to the Proxmox
host. It never asks an operator to restore over a working production install.

## Make a database backup

Run this from a trusted administration machine, not from the checked-out
repository's `volumes/` directory:

```sh
bash deploy/backup.sh prod /mnt/docker/_exports/deploy-backups
```

The script streams a PostgreSQL custom-format archive from the container,
validates it with `pg_restore --list`, and writes a SHA-256 sidecar. The output
directory is created with mode `0700`. The archive contains account and service
configuration, so copy it only to encrypted storage with restricted access.

The database is not a complete private-vault backup. Preserve these as one
consistent set:

- the verified database archive and its `.sha256` file;
- the vault volume's ciphertext files;
- the Library `.env` and deployment inventory, held in a protected secret
  store; and
- the user's recovery material and the original files until a restore has been
  verified.

Do not place Android signing material in the database archive. Store it in the
team's encrypted signing-material backup and record which Player release it
signs.

## Restore drill on a clean host

Perform this at least once before treating a vault backup as the only copy.
Use an isolated host, different Docker project name, empty PostgreSQL volume,
and copies of media/vault volumes. Do not attach the drill to production binds.

For the database portion, use the reproducible drill script first. It creates
an automatically removed PostgreSQL container with no network and a tmpfs data
directory; it does not read `.env`, connect through SSH or mount any media or
vault path. The archive must have the SHA-256 sidecar written by `backup.sh`:

```sh
bash deploy/recovery-drill.sh \
  /mnt/docker/_exports/deploy-backups/opus-library-prod-YYYYMMDDTHHMMSSZ.dump
```

It validates the archive, restores it into the empty disposable database, and
checks that the Alembic revision and at least one application table exist. Its
success proves only database restoration. It does not prove that copied media,
client-held vault keys or Android signing material are recoverable.

1. Check the saved archive before use:

   ```sh
   sha256sum -c opus-library-prod-YYYYMMDDTHHMMSSZ.dump.sha256
   pg_restore --list opus-library-prod-YYYYMMDDTHHMMSSZ.dump >/dev/null
   ```

2. Start only the clean database service. Restore into its empty `opus`
   database with the same PostgreSQL major version:

   ```sh
   pg_restore -U opus -d opus --clean --if-exists --no-owner --no-privileges \
     opus-library-prod-YYYYMMDDTHHMMSSZ.dump
   ```

3. Mount copies of the media, derivatives and vault volumes. Start Library and
   confirm `/api/ready`, the Alembic revision, catalogue counts and a sample
   of thumbnails. Do not run a cleanup sweep while checking copied media.

4. With the data owner's permission, unlock a sampled private vault in a real
   browser, restore files from an uninterrupted backup and one resumed backup,
   and compare their SHA-256 hashes with the originals. A database restore
   alone cannot establish that a user's client-side vault key is recoverable.

5. Record the command outputs, archive hash, image versions, migration
   revision, mounted volume copies, and the sampled restore results. Only after
   a successful drill may the previous database/archive set be retired.

## Production incident procedure

Freeze automatic import and landing cleanup first. Keep the affected volumes
read-only, capture logs and a fresh database archive, then restore the copy on
the isolated drill host. Decide the recovery point and only then schedule a
production maintenance window. Rebuilding application images or rerunning
Alembic is not a substitute for a tested database and vault recovery.
