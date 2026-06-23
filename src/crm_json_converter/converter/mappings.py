from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .models import FieldMapping, TableMapping


TABLE_MAPPINGS: dict[str, TableMapping] = {
    "customer": TableMapping(
        source_table="Customer",
        target_entity="Account",
        fields=(
            FieldMapping(
                "customer_id",
                "MUSE ID",
                "jh_thinkidnbr",
                "whole_number",
                minimum=0,
                maximum=1000000000,
                notes="Mapped to D365 Account field jh_thinkidnbr.",
            ),
            FieldMapping("address1", "Address Line 1", "address1_line1", "string", max_length=100),
            FieldMapping("address2", "Address Line 2", "address1_line2", "string", max_length=100),
            FieldMapping("address3", "Address Line 3", "address1_line3", "string", max_length=100),
            FieldMapping("company", "Account Name", "name", "string", max_length=100),
            FieldMapping(
                "department",
                "Department",
                "jh_department",
                "string",
                max_length=100,
                notes="Mapped to D365 Account field jh_department.",
            ),
            FieldMapping("city", "City", "address1_city", "string", max_length=100),
            FieldMapping("state_name", "State/Province", "address1_stateorprovince", "string", max_length=100),
            FieldMapping("country", "Country/Region", "address1_country", "string", max_length=100),
            FieldMapping(
                "country",
                "country",
                "jh_countryid",
                "string",
                lookup_target="jh_country",
                lookup_bind_entity_set="jh_countries",
                lookup_bind_key="jh_name",


            ),
            FieldMapping("zip", "ZIP/Postal Code", "address1_postalcode", "string", max_length=100),
            FieldMapping("email", "Email", "emailaddress1", "string", max_length=100),
            FieldMapping(
                "ringgold",
                "Ringgold ID",
                "jh_ringgoldidnbr",
                "whole_number",
                minimum=0,
                maximum=1_000_000_000,
                notes="Mapped to D365 Account field jh_ringgoldidnbr.",
            ),
            FieldMapping(
                "ringgold_parent",
                "Ringgold Parent ID",
                "jh_ringgoldparentidnbr",
                "whole_number",
                minimum=0,
                maximum=1_000_000_000,
                notes="Mapped to D365 Account field jh_ringgoldparentidnbr.",
            ),
        ),
    ),
    "agency": TableMapping(
        source_table="Agency",
        target_entity="Account",
        fields=(
            FieldMapping(
                "agency_customer_id",
                "MUSE ID",
                "jh_thinkidnbr",
                "whole_number",
                minimum=0,
                maximum=1000000000,
                notes="Mapped to D365 Account field jh_thinkidnbr.",
            ),
            FieldMapping("email", "Email", "emailaddress1", "string", max_length=100),
            FieldMapping("agency_bill_to", "Payment remitter", "jh_ispaymentremitter", "boolean"),
            FieldMapping("new_commission", "New Commission %", "jh_newcommissionpct", "decimal", minimum=0, maximum=100),
            FieldMapping("ren_commission", "Renewal Commission %", "jh_renewalcommissionpct", "decimal", minimum=0, maximum=100),
            FieldMapping("company", "Account Name", "name", "string", max_length=100),
            FieldMapping("fname", "Primary Contact", "primarycontactid", "string", lookup_target="contact", notes="Lookup stored as string only."),
            FieldMapping("initial_name", "Primary Contact", "primarycontactid", "string", lookup_target="contact", notes="Lookup stored as string only."),
            FieldMapping("lname", "Primary Contact", "primarycontactid", "string", lookup_target="contact", notes="Lookup stored as string only."),
            FieldMapping("suffix", "Primary Contact", "primarycontactid", "string", lookup_target="contact", notes="Lookup stored as string only."),
            FieldMapping("address1", "Address Line 1", "address1_line1", "string", max_length=100),
            FieldMapping("address2", "Address Line 2", "address1_line2", "string", max_length=100),
            FieldMapping("city", "City", "address1_city", "string", max_length=100),
            FieldMapping("state", "State/Province", "address1_stateorprovince", "string", max_length=100),
            FieldMapping("zip", "ZIP/Postal Code", "address1_postalcode", "string", max_length=100),
            FieldMapping("phone", "Main Phone", "telephone1", "string", max_length=100),
        ),
    ),
    "entitlement": TableMapping(
        source_table="Order Items",
        target_entity="jh_entitlement",
        fields=(
            FieldMapping(
                "orderhdr_id",
                "Entitlement ID",
                "jh_entitlementid",
                "string",
                notes="Deterministically derived GUID from orderhdr_id.",
            ),
            FieldMapping(
                "agency_customer_id",
                "Agent Account",
                "jh_agentaccountid",
                "string",
                lookup_bind_entity_set="accounts",
                lookup_bind_key="jh_thinkidnbr",
                notes="Lookup bound to D365 account key field jh_thinkidnbr.",
            ),
            FieldMapping(
                None,
                "Name",
                "jh_name",
                "string",
                max_length=20,
                notes="Copied from orderhdr_id for the entitlement name field.",
            ),
            FieldMapping("start_date", "Start Date", "jh_starton", "datetime", notes="Mapped to D365 entitlement start datetime field jh_starton."),
            FieldMapping("expire_date", "End Date", "jh_endon", "datetime", notes="Mapped to D365 entitlement end datetime field jh_endon."),
        ),
    ),
    "payment": TableMapping(
        source_table="Payment",
        target_entity="jh_entitlement",
        fields=(),
        d365_enabled=False,
    ),
    "order_item": TableMapping(
        source_table="Order Item",
        target_entity="jh_entitlementitems",
        d365_enabled=True,
        fields=(
            FieldMapping(
                "oc_desc",
                "Item",
                "jh_itemid_jh_collection",
                "string",
                lookup_bind_entity_set="jh_collections",
                lookup_bind_key="jh_name",
                notes="Lookup bound to D365 collection title field jh_name.",
            ),
            FieldMapping(
                "order_date", "jh_invoicedon", None, "datetime"
            ),
            FieldMapping(
                "orderhdr_id",
                "Entitlement",
                "jh_entitlementid",
                "string",
                lookup_bind_entity_set="jh_entitlements",
                lookup_bind_key="jh_entitlementid",
                notes="Lookup bound to D365 entitlement unique identifier field jh_entitlementid.",
            ),
            FieldMapping("start_date", "Start Date", None, "datetime", notes="Used to create the parent entitlement record."),
            FieldMapping("expire_date", "End Date", None, "datetime", notes="Used to create the parent entitlement record."),
            FieldMapping(
                None,
                "Name",
                "jh_name",
                "string",
                max_length=100,
                notes="Computed from orderhdr_id and order_item_seq as orderhdr_id:order_item_seq.",
            ),
            FieldMapping("description", "Description", None, "string", notes="Not pushed to D365 payload."),
            FieldMapping(
                "order_status",
                "Order Status",
                "jh_orderstatus",
                "optionset",
                options={
                    "Order Placed": 0,
                    "Canceled - Nonpayment": 1,
                    "Canceled - Customer Request": 2,
                    "Canceled - Credit Card Not Authorized": 3,
                    "Canceled - Audit Information Problem": 4,
                    "Active / Shipping": 5,
                    "Complete": 6,
                    "Grace Period": 7,
                    "Suspend - Nonpayment": 8,
                    "Suspend - Temporary": 9,
                    "Hold for Payment": 10,
                    "Suspended - Delivery Problem": 11,
                    "Suspended - Distribution Problem": 12,
                    "Suspended - Audit Information Problem": 13,
                    "Canceled - Audit Information Problem": 14,
                    "Hold Until Fulfillment Date": 15,
                    "Suspended - Behavior": 16,
                    "Suspended - Waiting Settle/Retry": 17,
                },
            ),
            FieldMapping(
                "payment_status",
                "Payment Status",
                "jh_paymentstatus",
                "optionset",
                options={
                    "No Payment": 0,
                    "Paid": 1,
                    "Paid - Overpayment": 2,
                    "Paid - Underpayment": 3,
                    "Paid - Prorated": 4,
                    "Partial Payment": 5,
                },
            ),
            FieldMapping(
                "order_item_seq",
                "Sequence",
                "jh_sequence",
                "whole_number",
                minimum=0,
                notes="Mapped to D365 entitlement item sequence field jh_sequence.",
            ),
            FieldMapping("fname", "Owner", None, "string", notes="Not pushed to D365 payload."),
            FieldMapping("lname", "Owner", None, "string", notes="Not pushed to D365 payload."),
            FieldMapping("company", "Owner", "ownerid", "string", notes="Lookup stored as string only."),
        ),
    ),
}


def normalize_table_name(table_name: str) -> str:
    normalized = table_name.strip().lower().replace(" ", "_")
    if normalized == "payment_item":
        return "order_item"
    return normalized


def get_supported_tables() -> list[str]:
    return sorted(TABLE_MAPPINGS.keys())


def get_table_mapping(table_name: str) -> TableMapping:
    key = normalize_table_name(table_name)
    if key not in TABLE_MAPPINGS:
        supported = ", ".join(get_supported_tables())
        raise ValueError(f"Unsupported table '{table_name}'. Supported tables: {supported}.")
    return TABLE_MAPPINGS[key]


def describe_table_mapping(table_name: str) -> dict[str, Any]:
    mapping = get_table_mapping(table_name)
    return {
        "source_table": mapping.source_table,
        "target_entity": mapping.target_entity,
        "d365_enabled": mapping.d365_enabled,
        "fields": [asdict(field) for field in mapping.fields],
    } 


def get_source_column_for_schema(table_name: str, crm_schema_name: str) -> str | None:
    mapping = get_table_mapping(table_name)
    for field in mapping.fields:
        if field.crm_schema_name == crm_schema_name:
            return field.source_column
    return None
