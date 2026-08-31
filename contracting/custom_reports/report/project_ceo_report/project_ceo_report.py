import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
    filters = filters or {}
    settings = frappe.get_single("CEO Report settings new")
    validate_settings(settings)

    columns = get_columns()
    data = get_data(settings, filters)
    return columns, data


# ─────────────────────────────────────────────
# COLUMNS — matches Financial_Report.xlsx "Financial Report" tab exactly
# ─────────────────────────────────────────────

def get_columns():
    return [
        {"label": _("اسم المشروع"), "fieldname": "project_label", "fieldtype": "Data", "width": 260},
        {"label": _("اجمالي قيمة العقد"), "fieldname": "total_contract_amount", "fieldtype": "Currency", "width": 150},
        {"label": _("الايرادات"), "fieldname": "invoiced_revenue", "fieldtype": "Currency", "width": 150},
        {"label": _("مكمل نسبة الاتمام"), "fieldname": "not_invoiced", "fieldtype": "Currency", "width": 160},
        {"label": _("اجمالي الايرادات المستحقة"), "fieldname": "total_earned_revenue", "fieldtype": "Currency", "width": 180},
        {"label": _("المتبقي من الاعمال"), "fieldname": "remaining_work", "fieldtype": "Currency", "width": 160},
        {"label": _("المحصل من الايراد"), "fieldname": "collected_revenue", "fieldtype": "Currency", "width": 160},
        {"label": _("المتبقي تحصيله من الايراد"), "fieldname": "remaining_to_collect", "fieldtype": "Currency", "width": 180},
        {"label": _("اجمالي المصروفات"), "fieldname": "total_expenses", "fieldtype": "Currency", "width": 150},
        {"label": _("الفرق بين المحصل والمصروفات"), "fieldname": "diff_collected_expenses", "fieldtype": "Currency", "width": 190},
        {"label": _("التوصية"), "fieldname": "recommendation", "fieldtype": "Data", "width": 220},
    ]


# ─────────────────────────────────────────────
# DATA — pulls live from GL Entry, no Excel involved
# ─────────────────────────────────────────────

NUMERIC_COLS = [
    "total_contract_amount", "invoiced_revenue", "not_invoiced",
    "total_earned_revenue", "remaining_work", "collected_revenue",
    "remaining_to_collect", "total_expenses", "diff_collected_expenses",
]


def get_data(settings, filters):
    revenue_accounts = [r.account for r in settings.revenue_accounts]
    collected_accounts = [r.account for r in getattr(settings, "collected_accounts", [])]
    discount_accounts = [r.account for r in settings.direct_cost_accounts if r.category == "Client Discount"]
    direct_cost_accounts = [r.account for r in settings.direct_cost_accounts if r.category == "Direct Cost"]

    rows = []
    for proj in settings.project_progress:
        cc = proj.cost_center

        def gl_balance(accounts, natural_debit=False):
            """SUM(credit-debit) for credit-normal accounts (income),
            SUM(debit-credit) for debit-normal accounts (asset/expense)."""
            if not accounts:
                return 0.0
            sign_expr = "debit - credit" if natural_debit else "credit - debit"
            result = frappe.db.sql(f"""
                SELECT SUM({sign_expr})
                FROM `tabGL Entry`
                WHERE account IN %(accounts)s
                    AND cost_center = %(cc)s
                    AND company = %(company)s
                    AND posting_date BETWEEN %(from_date)s AND %(to_date)s
                    AND is_cancelled = 0
            """, {
                "accounts": accounts,
                "cc": cc,
                "company": settings.company,
                "from_date": settings.from_date,
                "to_date": settings.to_date,
            })
            return flt(result[0][0] or 0.0)

        contract = flt(proj.total_contract_amount)
        pct = flt(proj.progress_percentage) / 100
        earned_value = contract * pct

        invoiced_revenue = abs(gl_balance(revenue_accounts))
        not_invoiced = earned_value - invoiced_revenue
        total_earned_revenue = invoiced_revenue + not_invoiced
        remaining_work = contract - total_earned_revenue

        collected_revenue = abs(gl_balance(collected_accounts, natural_debit=True))
        remaining_to_collect = total_earned_revenue - collected_revenue

        total_expenses = abs(gl_balance(direct_cost_accounts)) + abs(gl_balance(discount_accounts))
        diff_collected_expenses = collected_revenue - total_expenses

        recommendation = (
            _("يمكن الصرف على المشروع") if diff_collected_expenses >= 0
            else _("يجب تحصيل مستحقات أولاً")
        )

        rows.append({
            "project_label": proj.project_label or proj.cost_center,
            "total_contract_amount": contract,
            "invoiced_revenue": invoiced_revenue,
            "not_invoiced": not_invoiced,
            "total_earned_revenue": total_earned_revenue,
            "remaining_work": remaining_work,
            "collected_revenue": collected_revenue,
            "remaining_to_collect": remaining_to_collect,
            "total_expenses": total_expenses,
            "diff_collected_expenses": diff_collected_expenses,
            "recommendation": recommendation,
        })

    rows.append(make_totals_row(rows))
    return rows


def make_totals_row(rows):
    totals = {"project_label": _("الاجمالي"), "recommendation": ""}
    for col in NUMERIC_COLS:
        totals[col] = sum(flt(r.get(col, 0)) for r in rows)
    return totals


def validate_settings(settings):
    if not settings.project_progress:
        frappe.throw(_("Please add at least one project in Project Progress table."))
    if not settings.from_date or not settings.to_date:
        frappe.throw(_("Please set From Date and To Date in report settings."))
    if not settings.company:
        frappe.throw(_("Please select a Company."))
