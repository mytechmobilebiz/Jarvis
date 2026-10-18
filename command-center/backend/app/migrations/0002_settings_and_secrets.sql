-- Customer-facing settings (edited in the dashboard) and encrypted secrets
-- (customer API keys and credentials). Deployment/developer settings stay in
-- the environment and are never stored or shown here.

CREATE TABLE app_settings (
    key             TEXT PRIMARY KEY,
    value           JSONB NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      TEXT NOT NULL DEFAULT ''
);

-- Values are Fernet tokens (AES-128-CBC + HMAC-SHA256) under CC_SECRETS_KEY.
-- Only a short hint (last four characters) is ever returned by the API.
CREATE TABLE secret_values (
    name            TEXT PRIMARY KEY,
    ciphertext      TEXT NOT NULL,
    hint            TEXT NOT NULL DEFAULT '',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      TEXT NOT NULL DEFAULT ''
);
