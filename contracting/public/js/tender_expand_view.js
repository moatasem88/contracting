const EXPAND_VIEW_GRIDS = [
    {fieldname: 'boq_items', doctype: 'Tender BOQ Item'},
    {fieldname: 'material_items', doctype: 'Tender Material Item'},
    {fieldname: 'labor_items', doctype: 'Tender Labor Item'},
    {fieldname: 'subcontract_items', doctype: 'Tender Subcontract Item'},
    {fieldname: 'equipment_items', doctype: 'Tender Equipment Item'},
];

const AUTO_SYNC_SAVE_INTERVAL_MS = 2000;
const IDLE_CHECKPOINT_SAVE_MS = 8000;

// Module-scope so Expand View's dialog.on_hide can also trigger a
// checkpoint save on close, not just the idle timeout below.
function checkpoint_save(frm) {
    if (!frm.is_new() && frm.is_dirty() && !frappe.ui.form.is_saving) frm.save();
}

frappe.ui.form.on('Tender', {
    setup(frm) {
        // Auto-sync: push edited fields to the server roughly every 2s while
        // cell edits keep coming in, on either the native grid or Expand
        // View - both ultimately commit through frappe.model.set_value on
        // one of the four child doctypes, so one wildcard listener here
        // covers both UIs and all four tables without touching tender.js's
        // own field triggers. Throttle (not debounce): the first edit in a
        // burst syncs immediately (leading edge), further edits within the
        // window are collected into one trailing sync - continuous editing
        // still gets periodic syncs instead of waiting for a pause.
        // setup() is guarded by frm.setup_done in core and fires exactly
        // once per Form instance (see form.js), so this registers one
        // listener per child doctype for the tab's lifetime, not once per
        // document opened - re-registering on every Tender opened would
        // otherwise fire the same edit's sync multiple times.
        //
        // This calls contracting's sync_live_edits endpoint rather than
        // frm.save() - frm.save() hardcodes freeze: true on every request
        // (frappe/public/js/frappe/form/save.js's _call()) and does a full
        // frm.refresh() afterward, which is what used to paint a dark
        // overlay over the whole screen and steal focus out of whatever
        // cell was being typed into, every ~2s during active editing.
        // sync_live_edits persists only the touched rows' fields directly
        // and re-broadcasts doc_update the same way a real save would -
        // cross-session sync is unchanged, but nothing freezes or re-renders
        // the form on this path. A real frm.save() (freeze included, but
        // now rare rather than continuous) still runs at natural pause
        // points - see checkpoint_save, called from the idle timeout below
        // and from the Expand View dialog's on_hide.
        //
        // Deliberately NOT gated on frm.is_dirty(): this listener and
        // core's own dirty-flagging listener (form.js's watch_model_updates,
        // registered via the same frappe.model.on(doctype, '*', ...) API)
        // are both queued on the same wildcard event, and frappe.model.trigger
        // runs listeners serially in registration order - this one (from a
        // doctype_js "setup" script) registers before watch_model_updates's
        // (called later in Form.setup()), so at the instant this runs,
        // core's me.dirty() often hasn't fired yet and is_dirty() would
        // still read false. This only ever fires from an actual field
        // change, so the form is dirty (or about to be) regardless.
        let idle_checkpoint = null;
        let sync_save = frappe.utils.throttle(() => {
            if (frm.is_new()) return;
            // Suppress core's realtime doc_update handler (model.js) from
            // reacting to this call's own broadcast - mirrors the guard a
            // real frm.save() already sets around its own request/response
            // cycle (save.js). Without this, the originating tab hears its
            // own confirmed write back over the socket and misreads it as
            // an external change, which is what a Consolidate Rates by Item
            // propagated value "reverting a moment later" actually was.
            frappe.ui.form.is_saving = true;
            frappe.call({
                method: 'contracting.contracting.doctype.tender.tender.sync_live_edits',
                args: {tender: frm.doc},
                freeze: false,
                callback(r) {
                    if (!r.message) return;
                    // Direct assignment only - same idiom
                    // recalc_single_resource_row already uses for derived
                    // fields (row.amount = ..., not frappe.model.set_value)
                    // and for the same reason: a frappe.model.set_value here
                    // would re-fire this same wildcard listener. Keeping
                    // frm.doc.modified in step with the DB is also what
                    // stops the next real frm.save() (checkpoint_save) from
                    // hitting its own prior background write as a
                    // TimestampMismatchError.
                    frm.doc.modified = r.message.modified;
                    frm.doc.total_direct_cost = r.message.total_direct_cost;
                    frm.doc.total_additions = r.message.total_additions;
                    frm.doc.total_safety_factor = r.message.total_safety_factor;
                    frm.doc.tender_final_cost = r.message.tender_final_cost;
                    frm.doc.total_indirect_cost = r.message.total_indirect_cost;
                    frm.doc.sell_amount = r.message.sell_amount;
                },
                always() {
                    frappe.ui.form.is_saving = false;
                }
            });
        }, AUTO_SYNC_SAVE_INTERVAL_MS);

        EXPAND_VIEW_GRIDS.forEach(({doctype}) => {
            frappe.model.on(doctype, '*', (fieldname, value, doc) => {
                if (doc.parent !== frm.docname) return;
                sync_save();
                clearTimeout(idle_checkpoint);
                idle_checkpoint = setTimeout(() => checkpoint_save(frm), IDLE_CHECKPOINT_SAVE_MS);
            });
        });
    },
    refresh(frm) {
        // The dialog, grid buttons, column picker and cell editing are the
        // shared contracting.expand_view module (expand_view.js, also used
        // by Subcontractor Contract / Contractor Invoice / Addendum). What
        // stays here is only what Tender alone does.
        contracting.expand_view.attach(frm, EXPAND_VIEW_GRIDS, TENDER_EXPAND_VIEW_OPTIONS);
    }
});

function is_expand_view_field_editable(frm, docfield) {
    return !docfield.read_only
        && frm.perm[0].write
        && frm.doc.status !== 'Complete';
}

const TENDER_EXPAND_VIEW_OPTIONS = {
    // Tender has always edited every non-read-only cell as free text,
    // Link/Select included.
    is_editable: is_expand_view_field_editable,
    // Closing the dialog is one of the natural pause points for a real save
    // (see checkpoint_save above).
    on_hide: checkpoint_save,
    primary_action(frm, fieldname, child_doctype, ctx) {
        if (fieldname !== 'boq_items') return null;
        return {
            label: __('Add Item from Template'),
            handler() {
                open_add_boq_item_dialog(frm, () => {
                    // add_boq_item_from_template's frm.reload_doc() replaces
                    // frm.doc wholesale, so the old dialog's data/closures are
                    // stale by then anyway - reopening fresh lands the user
                    // back where they were.
                    ctx.reopen();
                });
            }
        };
    },
    after_render(datatable, rows, fieldname) {
        if (fieldname === 'boq_items') apply_expand_view_indent_styling(datatable, rows);
    }
};

function apply_expand_view_indent_styling(datatable, rows) {
    setTimeout(() => {
        let item_name_col_index = datatable.datamanager.columns.findIndex(c => c.id === 'item_name');
        if (item_name_col_index === -1) return;
        (rows || []).forEach((row, rowIndex) => {
            let $el = $(datatable.wrapper)
                .find(`.dt-cell[data-row-index="${rowIndex}"][data-col-index="${item_name_col_index}"] .dt-cell__content`);
            $el.css('padding-left', ((row.indent || 0) * 20) + 'px');
            if (row.is_group) $el.css('font-weight', 'bold');
        });
    }, 200);
}
