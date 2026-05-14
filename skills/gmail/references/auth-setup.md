# Gmail Skill — Authentication Setup

This skill uses a Google service account with **domain-wide delegation** to read mailboxes in a Google Workspace domain. One service account can impersonate any user in the domain whose mailbox you authorize it for.

## Prerequisites

- A Google Cloud project (any project will do — billing is not required for Gmail API read traffic at this scale).
- A Google Workspace domain where you have super-admin access.
- The mailbox owners' addresses you intend to read.

## Steps

### 1. Enable the Gmail API

In the Google Cloud Console for your project: **APIs & Services → Library → Gmail API → Enable**.

### 2. Create the service account

**IAM & Admin → Service Accounts → Create service account**. Give it a name like `pillbug-gmail-reader`. No project-level roles are needed — Gmail access is granted via domain-wide delegation, not IAM.

### 3. Create and download a JSON key

On the new service account: **Keys → Add Key → Create new key → JSON**. The downloaded file is the credential used by the helper.

### 4. Enable domain-wide delegation

On the service account's detail page: **Show advanced settings → Domain-wide delegation → Enable**. Note the **client ID** (a long numeric string) that appears — you need it in the next step.

### 5. Authorize the scope in Workspace Admin

Sign in to <https://admin.google.com> as a super-admin and go to **Security → Access and data control → API controls → Domain-wide delegation → Add new**.

- **Client ID:** the numeric client ID from step 4.
- **OAuth scopes:** `https://www.googleapis.com/auth/gmail.readonly`

Save. The authorization may take a few minutes to propagate.

### 6. Place the key file

Copy the JSON key to the Pillbug runtime base dir:

```bash
cp ~/Downloads/pillbug-gmail-reader-*.json ~/.pillbug/gmail_service_account.json
chmod 600 ~/.pillbug/gmail_service_account.json
```

Or set `PB_GMAIL_SERVICE_ACCOUNT_PATH` to a custom location (absolute path, or relative to `PB_BASE_DIR`).

### 7. Install the optional dependency

```bash
uv sync --extra gmail
```

### 8. Verify

```bash
uv run python skills/gmail/scripts/gmail_cli.py list you@yourdomain.com --max 1
```

You should get a JSON envelope with one message. If you see exit 3 (auth/config) or exit 4 (Gmail API), the most common causes are:

- The JSON key path is wrong → check `PB_GMAIL_SERVICE_ACCOUNT_PATH` and the file exists.
- The scope was not authorized in step 5, or authorization is still propagating → wait a few minutes and retry.
- The target address is not in the same Workspace domain that authorized the delegation → domain-wide delegation only grants access within the authorizing domain.

## Security notes

- The JSON key grants read access to **every mailbox in the domain** for which the scope is authorized. Treat it like a master password: file mode 600, no version control, no plaintext sharing.
- Revoking access: delete the JSON key in the Cloud Console, or remove the delegation entry in Workspace Admin.
- The helper requests only the `gmail.readonly` scope. It cannot send or modify mail even if the credential were misused inside this runtime.
