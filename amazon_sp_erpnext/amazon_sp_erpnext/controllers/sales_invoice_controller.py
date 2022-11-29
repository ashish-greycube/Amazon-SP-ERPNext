# Copyright (c) 2022, Greycube and contributors
# For license information, please see license.txt

import frappe
import pandas as pd
import io, os, json
import dateutil
from frappe.model.naming import get_default_naming_series
from frappe.utils import cstr
from amazon_sp_erpnext.amazon_sp_erpnext.controllers.mtr import MTR_COLUMNS as mcols
import zipfile
from frappe.utils.csvutils import read_csv_content
from erpnext.controllers.accounts_controller import (
    add_taxes_from_tax_template,
)


def amz_datetime_to_date(amz_date):
    return dateutil.parser.parse(amz_date).strftime("%Y-%m-%d")


def amz_datetime_to_time(amz_date):
    return dateutil.parser.parse(amz_date).strftime("%H-%M-%s")


def get_naming_series(amz_setting):
    return amz_setting.sales_invoice_series or get_default_naming_series(
        "Sales Invoice"
    )


def get_b2c_customer():
    return frappe.get_cached_value(
        "Amazon SP Common Settings", None, "amazon_customer_for_b2c"
    )


def get_warehouse(fc_name, company):
    warehouse = frappe.db.get_all(
        "Warehouse",
        pluck="name",
        filters={"amazon_fba_fulfilment_center": fc_name, "company": company},
        limit=1,
    )
    return warehouse and warehouse[0][0] or None


def make_contacts(data):
    # create contacts for B2C customers
    amazon_customer_for_b2c = get_b2c_customer()

    contacts = frappe.db.get_all(
        "Dynamic Link",
        filters={
            "link_doctype": "Customer",
            "link_name": amazon_customer_for_b2c,
            "parenttype": "Contact",
        },
        pluck="parent",
    )

    for d in data:
        if f"Buyer-{d}-{amazon_customer_for_b2c}" in contacts:
            return
        print("creating: ", d)
        frappe.get_doc(
            {
                "doctype": "Contact",
                "first_name": f"Buyer-{d}",
                "links": [
                    {
                        "doctype": "Dynamic Link",
                        "link_doctype": "Customer",
                        "link_name": amazon_customer_for_b2c,
                    },
                ],
            }
        ).insert()


def make_address(df):
    nowtime = frappe.utils.now()
    df_copy = (
        df[
            [
                "Order Id",
                "Ship To City",
                "Ship To State",
                "Ship To Country",
                "Ship To Postal Code",
            ]
        ]
        .copy()
        .rename(
            columns={
                "Order Id": "order-id",
                "Ship To City": "city",
                "Ship To State": "state",
                "Ship To Country": "country",
                "Ship To Postal Code": "pincode",
            }
        )
    )

    df_copy.drop_duplicates(inplace=True, ignore_index=True)
    df_copy = df_copy.assign(
        name=df_copy["order-id"] + " - Shipping",
        creation=nowtime,
        modified=nowtime,
        modified_by="Administrator",
        address_type="Shipping",
        address_line1="Not Provided",
        country=lambda x: "India" if x["country"].str == "IN" else x["country"],
    )

    fields = [
        "name",
        "creation",
        "modified",
        "modified_by",
        "address_type",
        "address_line1",
        "city",
        "state",
        "pincode",
        "country",
    ]

    data = df_copy[fields].values.tolist()
    frappe.db.bulk_insert(
        "Address",
        fields=fields,
        values=data,
        ignore_duplicates=True,
    )

    frappe.db.commit()


def make_b2b_customer_contact(amz_setting, args={}):
    # create B2B Customer

    customer = frappe.get_doc(
        {
            "doctype": "Customer",
            "customer_name": args.get("Buyer Name"),
            "customer_group": amz_setting.customer_group,
            "territory": amz_setting.territory,
            "customer_type": amz_setting.customer_type,
        }
    )
    customer.save()

    contact = frappe.get_doc(
        {
            "doctype": "Contact",
            "first_name": args.get("Buyer Name"),
            "links": [
                {
                    "doctype": "Dynamic Link",
                    "link_doctype": "Customer",
                    "link_name": customer.name,
                },
            ],
        }
    ).insert()
    return customer.name, contact.name


def get_mtr_df(file_name=None, amz_setting=None):
    amz_setting = frappe.get_cached_doc("Amazon SP Settings", amz_setting)
    is_b2b, df, items = False, None, []

    if not file_name:
        for d in frappe.get_all(
            "File",
            {"file_name": ["like", "GST_MTR_B2%"]},
            order_by="creation desc",
            limit=1,
        ):
            file_name = d.name

    if not file_name:
        frappe.throw("No MTR `file to process.")

    file_doc = frappe.get_doc("File", file_name)
    content = file_doc.get_content()

    if not isinstance(content, str):
        # read signature to check if zip file

        hex_signature = " ".join(["{:02X}".format(byte) for byte in content[:4]])
        if hex_signature in ["50 4B 03 04", "50 4B 05 06", "50 4B 07 07"]:
            _file = "{}{}".format(
                os.path.abspath(frappe.get_site_path()), file_doc.file_url
            )
            with zipfile.ZipFile(_file) as z:
                for file in z.filelist:
                    content = str(z.read(file.filename))

    df = pd.read_csv(
        io.StringIO(content),
        # sep="\t",
    )
    return df[(df[mcols.TRANSACTION_TYPE] == "Shipment") & (df[mcols.QUANTITY] > 0)]


def process_mtr_file(file_name=None, amz_setting=None, submit=True):
    df = get_mtr_df(file_name, amz_setting)
    amz_setting = frappe.get_cached_doc("Amazon SP Settings", amz_setting)
    amz_common = frappe.get_single("Amazon SP Common Settings")

    is_b2b = mcols.CUSTOMER_BILL_TO_GSTID in df.columns

    if not is_b2b:
        contacts = df[mcols.ORDER_ID].drop_duplicates().to_list()
        # make_contacts(contacts)

    make_address(df)

    customer_name = get_b2c_customer()
    contact_name = ""

    for order_id in set(df[mcols.ORDER_ID].values.tolist()):
        print("\n\ncreating sales invoice: %s" % (order_id))
        df_copy = df[df[mcols.ORDER_ID] == order_id]
        if not len(df_copy):
            frappe.throw("No Order lines to import in file.")

        lines = df_copy.to_dict("records")
        try:
            order = lines[0]
            order_id = order.get(mcols.ORDER_ID)
            # check for duplicate
            if frappe.db.exists("Sales Invoice", {"amazon_order_id_cf": order_id}):
                return

            if is_b2b:
                customer_name, contact_name = make_b2b_customer_contact(
                    amz_setting, order
                )

            posting_date = amz_datetime_to_date(order.get(mcols.INVOICE_DATE))
            warehouse = frappe.db.get_value(
                "Warehouse",
                {"amazon_fba_fulfilment_center": order.get(mcols.WAREHOUSE_ID)},
            )

            args = {
                "doctype": "Sales Invoice",
                "naming_series": get_naming_series(amz_setting),
                "company": amz_setting.company,
                "posting_date": posting_date,
                "amazon_order_id_cf": order_id,
                "debit_to": amz_setting.default_receivable_account,
                "customer": customer_name,
                "tax_id": order.get(mcols.CUSTOMER_BILL_TO_GSTID),
                "company_tax_id": order.get(mcols.SELLER_GSTIN),
                "posting_time": amz_datetime_to_time(order.get(mcols.INVOICE_DATE)),
                "due_date": posting_date,  # order already paid and shipped in amazon
                # "return_against":None,
                "cost_center": amz_setting.default_cost_center,
                "po_no": order.get(mcols.ORDER_ID),
                "po_date": order.get(mcols.ORDER_DATE),
                "billing_address_gstin": order.get(mcols.CUSTOMER_BILL_TO_GSTID),
                # "customer_address": order.get(mcols.BUYER_NAME),
                # "contact_person": contact_name,
                # "shipping_address_name": order.get(mcols.BUYER_NAME),
                "customer_gstin": order.get(mcols.CUSTOMER_BILL_TO_GSTID),
                "place_of_supply": order.get(mcols.SHIP_TO_STATE),
                "territory": amz_setting.territory,
                # "company_address": "",  # From Address doctype : Based on Seller GSTIN
                "company_gstin": order.get(mcols.SELLER_GSTIN),
                "port_of_loading": "",
                "set_warehouse": warehouse,
                "total_qty": sum([d.get(mcols.QUANTITY) for d in lines]),
                "tax_category": amz_common.get_tax_category(order),
                "gst_category": amz_common.get_gst_category(order),
                "irn": order.get("IRN_NUMBER"),
                "items": [],
            }
            sales_invoice = frappe.get_doc(args)

            for d in lines:
                if not d.get(mcols.QUANTITY):
                    continue

                item_details = frappe.db.get_value(
                    "Item",
                    d.get(mcols.SKU),
                    ["item_code", "item_name", "description", "stock_uom"],
                    as_dict=True,
                )

                if not item_details:
                    frappe.throw(
                        "Invalid Item Code: {} in Order Id: {}".format(
                            d.get(mcols.SKU), order_id
                        )
                    )

                sales_invoice.append(
                    "items",
                    {
                        "item_code": item_details.item_code,
                        "item_name": item_details.item_name,
                        "description": item_details.description,
                        "gst_hsn_code": d.get(mcols.HSN_SAC),
                        "rate": 0,
                        "net_rate": 0,
                        "qty": d.get(mcols.QUANTITY) or 0,
                        "delivered_qty": d.get(mcols.QUANTITY) or 0,
                        "stock_uom": item_details.stock_uom,
                        "conversion_factor": "1.0",
                        "warehouse": get_warehouse(
                            d.get(mcols.WAREHOUSE_ID), amz_setting.company
                        ),
                        "item_tax_template": amz_common.get_item_tax_template(order),
                    },
                )

            sales_invoice.transaction_date = sales_invoice.posting_date
            from erpnext.stock.get_item_details import get_item_tax_map

            for d in sales_invoice.items:
                d.item_tax_rate = get_item_tax_map(
                    sales_invoice.get("company"), d.item_tax_template, as_json=True
                )
                add_taxes_from_tax_template(d, sales_invoice, False)

            sales_invoice.insert(ignore_permissions=True)

            if submit:
                sales_invoice.submit()

            make_log(lines, order_id, sales_invoice.name)
            print("Created invoice: ", sales_invoice.name)
            # break
        except Exception as e:
            make_log(lines, order_id, sales_invoice=None, error=cstr(e))
            frappe.log_error(
                title="Error creating invoice for %s" % order_id,
                message=frappe.get_traceback(),
            )
            print(e)


def make_log(order_lines, order_id, sales_invoice=None, error=None):
    for d in order_lines:
        frappe.get_doc(
            {
                "doctype": "Amazon Order Log",
                "amazon_order_id": order_id,
                "status": error and "Error" or "Processed",
                "sales_invoice": sales_invoice,
                "error": error,
                "amazon_order_json": json.dumps(d),
            }
        ).insert()
