# Ledger dry run: one-off, keyless, read-only (Nov 2026)

Before the email-reliability release (M2), we need to know how it will read
the existing personal alert ledgers (`users/{uid}/deliveries` in
Firestore). `.github/workflows/ledger_dryrun.yml` gets those counts.

**How it works.**

* **No key.** It never uses the Firebase admin key (`FIREBASE_SERVICE_ACCOUNT`),
  and no key is created for it.
* **Short-lived identity.** GitHub gives the run a short-lived identity
  token. Google exchanges it (Workload Identity Federation) for
  **ledger-reader**, a service account whose only role is a custom role with
  two permissions:
  * `datastore.entities.get`: read documents.
  * `datastore.entities.list`: list documents and run queries.

  Google itself refuses any write.
* **A second layer.** The pinned dry-run code also blocks every Firestore
  write path.
* **Locked to one workflow.** Google accepts the exchange only from this
  repository, identified by its numeric ID `1291099098` and owner ID
  `242191838`, its `main` branch, and this workflow file, started by hand.
  Pinning the numeric IDs means a different repository that later takes the
  same name can't qualify.
* **Counts only in the public log.** No UID, address, job or document id.

Everything below is done in a browser: the Google Cloud console, Cloud
Shell, and GitHub. Nothing runs on your own computer.

* `PROJECT_ID` is your Firebase project ID. In the Firebase console it's
  under *Project settings → General*.
* `PROJECT_NUMBER` is the project number shown next to it.

## 1. Create the read-only reader, without a key

Open **Cloud Shell** (the `>_` button in the Google Cloud console), then set
the project:

```bash
gcloud config set project PROJECT_ID
```

Enable the APIs the exchange uses. If they're already on, this changes
nothing.

```bash
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com
```

Create the reader role with only two permissions. You can check that both
are allowed in custom roles with
`gcloud iam list-testable-permissions //cloudresourcemanager.googleapis.com/projects/PROJECT_ID --filter="name:datastore.entities.get OR name:datastore.entities.list" --format="table(name,customRolesSupportLevel,stage)"`.
A blank support level means supported.

```bash
gcloud iam roles create ledgerReader --project=PROJECT_ID --title="Ledger reader (read-only)" --description="Read and list Firestore documents only" --permissions=datastore.entities.get,datastore.entities.list --stage=GA
```

Create the service account:

```bash
gcloud iam service-accounts create ledger-reader --display-name="Ledger dry run (read-only)"
```

Give it that role, and nothing else:

```bash
gcloud projects add-iam-policy-binding PROJECT_ID --member="serviceAccount:ledger-reader@PROJECT_ID.iam.gserviceaccount.com" --role="projects/PROJECT_ID/roles/ledgerReader"
```

**Never create a key for `ledger-reader`.** It doesn't need one.

## 2. Trust this workflow only (Workload Identity Federation)

Create the identity pool:

```bash
gcloud iam workload-identity-pools create github-ledger --location=global --display-name="Ledger dry run"
```

Create the provider. It maps GitHub's token claims to attributes. Its
attribute condition is the lock: this repository and owner, by name and by
numeric ID, the `main` branch, a manual run, and this workflow file.

```bash
gcloud iam workload-identity-pools providers create-oidc github-main --location=global --workload-identity-pool=github-ledger --issuer-uri="https://token.actions.githubusercontent.com" --attribute-mapping="google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.workflow_ref=assertion.workflow_ref,attribute.event_name=assertion.event_name" --attribute-condition="assertion.repository_id=='1291099098' && assertion.repository_owner_id=='242191838' && assertion.repository=='manojanumolu/job-tracker' && assertion.ref=='refs/heads/main' && assertion.event_name=='workflow_dispatch' && assertion.workflow_ref=='manojanumolu/job-tracker/.github/workflows/ledger_dryrun.yml@refs/heads/main'"
```

Let only identities from this repository, matched by its numeric ID, act as
the reader:

```bash
gcloud iam service-accounts add-iam-policy-binding ledger-reader@PROJECT_ID.iam.gserviceaccount.com --role="roles/iam.workloadIdentityUser" --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-ledger/attribute.repository_id/1291099098"
```

This is the reader's only impersonation grant. The pool's identities get no
other role anywhere.

## 3. Add two repository secrets

These two values are identifiers, not credentials. Storing them as secrets
means the log masks them.

In GitHub, go to *Settings → Secrets and variables → Actions → New repository
secret*:

| Name | Value |
|---|---|
| `LEDGER_WIF_PROVIDER` | `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-ledger/providers/github-main` |
| `LEDGER_READER_SERVICE_ACCOUNT` | `ledger-reader@PROJECT_ID.iam.gserviceaccount.com` |

The workflow refuses any other shape, and it refuses any
`firebase-adminsdk…` account outright.

## 4. Run once (after approval)

1. Merge `claude/m2-rollback-compat`, then `claude/ledger-dryrun-workflow`,
   into `main`.
2. Go to *Actions → Ledger dry run (read-only, one-off) → Run workflow*
   (branch `main`).

The log ends with counts per account (#1–#3) and in total:

* records and old-format records;
* counts by state;
* abandoned claims;
* how many would become uncertain, and how many would be retried;
* skipped user documents.

If the exchange or a permission fails, the step stops with an error type
only. Nothing is read or written.

## 5. Remove everything afterwards

1. Revert the workflow's merge on `main`. This must happen **before** the
   GitHub security release (M3) is merged.
2. In GitHub (*Settings → Secrets and variables → Actions*), delete
   `LEDGER_WIF_PROVIDER` and `LEDGER_READER_SERVICE_ACCOUNT`.
3. In Cloud Shell, delete the provider and the pool. Then remove the
   reader's role binding, and delete the service account and the custom
   role.

   ```bash
   gcloud iam workload-identity-pools providers delete github-main --location=global --workload-identity-pool=github-ledger --quiet
   ```
   ```bash
   gcloud iam workload-identity-pools delete github-ledger --location=global --quiet
   ```
   ```bash
   gcloud projects remove-iam-policy-binding PROJECT_ID --member="serviceAccount:ledger-reader@PROJECT_ID.iam.gserviceaccount.com" --role="projects/PROJECT_ID/roles/ledgerReader"
   ```
   ```bash
   gcloud iam service-accounts delete ledger-reader@PROJECT_ID.iam.gserviceaccount.com --quiet
   ```
   ```bash
   gcloud iam roles delete ledgerReader --project=PROJECT_ID --quiet
   ```

## Why not the existing key

`FIREBASE_SERVICE_ACCOUNT` is the Firebase **admin** key: it can write
everything. Only the code would keep a run read-only. With the reader,
Google's permissions refuse writes even if the code were wrong. There is
also no key to leak, since the GitHub token expires within minutes.
