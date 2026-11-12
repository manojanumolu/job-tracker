# Ledger dry run: one-off, keyless, read-only (Nov 2026)

Before the email-reliability release (M2), we need to know how it will read
the existing personal alert ledgers (`users/{uid}/deliveries` in
Firestore). `.github/workflows/ledger_dryrun.yml` gets those counts.

**What the workflow does**

* **No key.** It never uses the Firebase admin key (`FIREBASE_SERVICE_ACCOUNT`)
  and creates no other key.
* **Short-lived identity.** GitHub gives the run a short-lived identity
  token. Google exchanges it for **ledger-reader**, a service account that
  holds only **Cloud Datastore Viewer**. So Google itself refuses any write.
* **Second guard in the code.** The pinned dry-run code also blocks every
  Firestore write path, as a second layer.
* **Locked to one workflow.** Google accepts the exchange only from this
  repository's `main` branch, this workflow file, started by hand.
* **Counts only in the public log.** No UID, address, job or document id.

Everything below is done in a browser: the Google Cloud console, Cloud
Shell, and GitHub. Nothing runs on your own computer. Replace `PROJECT_ID`
with your Firebase project ID, and `PROJECT_NUMBER` with its project number
(Cloud console → project picker, or *Project settings* in Firebase).

## 1. Create the read-only reader, without a key

Open **Cloud Shell** (the `>_` button in the Google Cloud console). Set the
project:

```bash
gcloud config set project PROJECT_ID
```

Enable the two APIs the exchange uses:

```bash
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com
```

Create the reader:

```bash
gcloud iam service-accounts create ledger-reader --display-name="Ledger dry run (read-only)"
```

Give it the read-only role:

```bash
gcloud projects add-iam-policy-binding PROJECT_ID --member="serviceAccount:ledger-reader@PROJECT_ID.iam.gserviceaccount.com" --role="roles/datastore.viewer"
```

**Do not create a key for it.** It doesn't need one.

## 2. Trust this workflow only (Workload Identity Federation)

Create the identity pool:

```bash
gcloud iam workload-identity-pools create github-ledger --location=global --display-name="Ledger dry run"
```

Create the provider. Its attribute condition is the lock: it accepts only
this workflow file, on `main`, started by hand.

```bash
gcloud iam workload-identity-pools providers create-oidc github-main --location=global --workload-identity-pool=github-ledger --issuer-uri="https://token.actions.githubusercontent.com" --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.workflow_ref=assertion.workflow_ref,attribute.event_name=assertion.event_name" --attribute-condition="assertion.repository=='manojanumolu/job-tracker' && assertion.ref=='refs/heads/main' && assertion.event_name=='workflow_dispatch' && assertion.workflow_ref=='manojanumolu/job-tracker/.github/workflows/ledger_dryrun.yml@refs/heads/main'"
```

Let only identities from this repository act as the reader:

```bash
gcloud iam service-accounts add-iam-policy-binding ledger-reader@PROJECT_ID.iam.gserviceaccount.com --role="roles/iam.workloadIdentityUser" --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-ledger/attribute.repository/manojanumolu/job-tracker"
```

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

If the exchange fails (a typo in a secret or a binding), the step stops
with an error type only. Nothing is read or written.

## 5. Remove everything afterwards

1. Revert the workflow's merge on `main`. This must happen **before** the
   GitHub security release (M3) is merged.
2. Delete the two repository secrets.
3. In Cloud Shell, delete the provider, the pool and the reader:

   ```bash
   gcloud iam workload-identity-pools providers delete github-main --location=global --workload-identity-pool=github-ledger --quiet
   ```
   ```bash
   gcloud iam workload-identity-pools delete github-ledger --location=global --quiet
   ```
   ```bash
   gcloud iam service-accounts delete ledger-reader@PROJECT_ID.iam.gserviceaccount.com --quiet
   ```

   The reader's role binding is removed together with the account. A
   deleted pool stays recoverable for 30 days, then is purged.

## Why not the existing key

`FIREBASE_SERVICE_ACCOUNT` is the Firebase **admin** key: it can write
everything. Only the code would keep a run read-only. With the reader,
Google's permissions refuse writes even if the code were wrong. There is
also no key to leak, since the GitHub token expires within minutes.
