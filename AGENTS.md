# AGENTS.md — local rules for coding agents

These rules apply to every coding agent touching this repository (human or
AI), regardless of session.

---

## Never modify `.env` to run tests

`.env` is the user's local configuration with real (or test) credentials.
Re-writing it for ad-hoc test scenarios has already wiped the user's
settings twice. **Do not do that.**

### How to test without touching `.env`

Pick the right tool for the experiment:

1. **Override a single variable in shell** — fastest, no files touched.
   `UV_ENV_FILE=.env` keeps loading the user's `.env`; the shell value
   wins because OS env takes precedence over both `UV_ENV_FILE` and
   pydantic-settings' `env_file`.

   ```bash
   # "What if no Yandex creds at all?"
   STT_PROXY_YANDEX_API_KEY= STT_PROXY_YANDEX_FOLDER_ID= task run

   # "What if SaluteSpeech is also enabled?"
   STT_PROXY_SALUTESPEECH_KEY=test task run

   # "Bind to a different port just for this run"
   STT_PROXY_PORT=15000 task run
   ```

2. **Use `.env.test.local`** for a complete different credential set.
   The file is git-ignored, exists only on the agent's machine, and is
   loaded with `UV_ENV_FILE=.env.test.local task run` (the
   `UV_ENV_FILE` env var is overridden at the shell level — the
   Taskfile's hard-coded `UV_ENV_FILE: .env` is bypassed).

   ```bash
   cat > .env.test.local <<EOF
   STT_PROXY_YANDEX_API_KEY=dummy
   STT_PROXY_YANDEX_FOLDER_ID=dummy
   STT_PROXY_SALUTESPEECH_KEY=dummy
   EOF

   UV_ENV_FILE=.env.test.local task run
   rm .env.test.local        # cleanup
   ```

3. **`task dev:info`** — quick check of what providers the current
   `.env` would enable, no server start needed.

### Last resort: if `.env` itself must be modified

Only after explicit user permission, and **always** with a timestamped
backup:

```bash
BACKUP=".env.bak.$(date +%s)"
cp .env "$BACKUP"

# ... do whatever was authorised ...

cp "$BACKUP" .env
diff "$BACKUP" .env && echo "restored OK" || echo "MISMATCH — investigate"
rm "$BACKUP"
```

If the diff at the end shows any difference, **stop and tell the user**
— do not silently leave a corrupted `.env` behind.

---

## Why these rules exist

- `cat > .env <<EOF` is a destructive operation; if anything between the
  write and the restore fails (interrupted command, killed shell, agent
  crash), the user's settings are gone.
- `.env.test.local` is git-ignored, so it cannot leak credentials into
  the repository even by accident.
- Shell env overrides are visible in `ps eww` and survive across
  multiple commands; they don't depend on file lifecycle.