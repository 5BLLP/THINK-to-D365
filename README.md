# THINK to D365

Transform source CRM JSON into Dynamics 365 payloads, write the mapped output, and optionally push sanitized records to D365.

## Architecture

This project supports five source tables and turns source CRM JSON into D365-ready payloads.

| Source table | D365 target | Notes |
|---|---|---|
| `customer` | `Account` | Maps customer records into D365 accounts, including addresses, contact details, and ringgold fields. `jh_thinkidnbr` is the account key. |
| `agency` | `Account` | Maps agency records into D365 accounts, including commission fields and contact lookup behavior. `jh_thinkidnbr` is the account key. |
| `entitlement` | `jh_entitlement` | Maps order-item entitlement fields into D365 entitlements. `jh_entitlementid` comes from `orderhdr_id`; `jh_starton` / `jh_endon` come from `start_date` / `expire_date`. |
| `payment` | `jh_entitlement` | Reserved for future use. The mapper is currently disabled. |
| `payment_item` | `jh_entitlementitems` | Maps order item records into entitlement items. The parent entitlement is resolved from `orderhdr_id`; `jh_name` is computed from the composite `orderhdr_id:order_item_seq`; `order_status` / `payment_status` are numeric choice fields; `jh_sequence` comes from `order_item_seq`. |

Note:

- `jh_thinkidnbr` is the key used for customer and agency account matching.
- The current mapper does not emit `jh_museid` for customer or agency records.
- `orderhdr_id` is the entitlement key used to build `jh_entitlementid` for `entitlement`.
- `orderhdr_id:order_item_seq` is the composite key used to build `jh_name` for `payment_item`.
- For `payment_item`, that composite value is also used when deduping source rows before lookup and write.

## Developer Workflow

### Requirements

- Python 3.10+
- A `.env` file with D365 connection settings
- Network access to the target D365 environment over HTTPS

### Install

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### Configuration

The application loads environment variables from `.env` in the project root.

Required values:

```env
D365_TENANT_ID=...
D365_CLIENT_ID=...
D365_CLIENT_SECRET=...
D365_RESOURCE_URL=...

D365_CUSTOMER_ENTITY_SET=...
D365_CUSTOMER_MATCH_FIELD=...
D365_CUSTOMER_PRIMARY_ID_FIELD=...

D365_AGENCY_ENTITY_SET=...
D365_AGENCY_MATCH_FIELD=...
D365_AGENCY_PRIMARY_ID_FIELD=...

D365_PAYMENT_ENTITY_SET=...
D365_PAYMENT_MATCH_FIELD=...
D365_PAYMENT_PRIMARY_ID_FIELD=...

D365_PAYMENT_ITEM_ENTITY_SET=...
D365_PAYMENT_ITEM_MATCH_FIELD=...
D365_PAYMENT_ITEM_PRIMARY_ID_FIELD=...
```

Optional batch and logging settings:

```env
D365_BATCH_ENABLED=true
D365_BATCH_PARALLEL=true
D365_BATCH_SIZE=50
D365_BATCH_MAX_WORKERS=4
D365_BATCH_RETRY_ATTEMPTS=10

D365_LOG_DIR=logs
# D365_LOG_PATH=logs/crm_push.log
```

### CLI

The entrypoint is [`transform.py`](transform.py), which wraps the package CLI.

Supported flags:

- `--table`
- `--input`
- `--output`
- `--pretty`
- `--list-tables`
- `--describe`
- `--push-d365`
- `--debug-http`

Flag rules:

- `--table` is required unless `--list-tables` is used.
- `--input` is required unless `--describe` is used.
- `--debug-http` requires `--push-d365`.
- `--describe` cannot be combined with `--push-d365` or `--debug-http`.

### Examples

List supported tables:

```bash
python transform.py --list-tables --pretty
```

Describe a table:

```bash
python transform.py --table entitlement --describe --pretty
```

Describe a table:

```bash
python transform.py --table payment_item --describe --pretty
```

Transform only:

```bash
python transform.py --table customer --input samples/customer.json --pretty
```

Write to a custom output file:

```bash
python transform.py --table customer --input samples/customer.json --output output/customer_ready.json --pretty
```

Transform and push to D365:

```bash
python transform.py --table payment_item --input samples/payment_items.json --push-d365 --pretty
```

Push with HTTP debugging:

```bash
python transform.py --table payment_item --input samples/payment_items.json --push-d365 --debug-http --pretty
```

Before pushing, place real source files in `samples/` and point `--input` at those files. The repository keeps `samples/` available for local data, but the actual input files are intentionally ignored by git.

### Behavior

- Sanitization happens before the D365 push.
- The mapped JSON output is always written, even when `--push-d365` is used.
- Batch mode processes lookups and writes in chunks.
- `entitlement` dedupes source rows before lookup/write using `orderhdr_id`.
- `payment_item` first writes or updates entitlements, then writes entitlement items.
- `payment_item` dedupes source rows before lookup/write using `orderhdr_id` and `order_item_seq`.
- `payment_item` writes a computed `jh_name` value and a numeric `jh_sequence`.
- Duplicate source rows are skipped during batch processing.
- If a D365 lookup or write fails, the run logs the failure and keeps going where possible.

### Logging

If `D365_LOG_PATH` is not set, logs are written to:

```text
logs/crm_push_YYYY-MM-DD.log
```

Common event types:

- `push_start`
- `lookup_queued`
- `http_result`
- `record_upsert`
- `push_complete`

Each run has a `run_id` so related log lines can be grouped.

### Repository Layout

```text
transform.py
src/crm_json_converter/
tests/
samples/
output/
logs/
```

The `samples/`, `output/`, and `logs/` folders are intended for local files and run artifacts. Put real test or source data in `samples/` before running transform or push commands.

### Testing

Run the full test suite with:

```bash
python -m unittest discover -s tests
```

## Contributing

Do not commit your `.env` file. It contains credentials and tenant-specific settings.
