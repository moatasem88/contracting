const EXPAND_VIEW_GRIDS = [
    {fieldname: 'boq_items', doctype: 'Tender BOQ Item'},
    {fieldname: 'material_items', doctype: 'Tender Material Item'},
    {fieldname: 'labor_items', doctype: 'Tender Labor Item'},
    {fieldname: 'equipment_items', doctype: 'Tender Equipment Item'},
];

// Tracks any Expand View dialogs currently open, keyed by grid fieldname -
// frm's own refresh() (which also fires for the realtime doc_update
// auto-reload frappe.model.init() wires up globally, see model.js's
// `frappe.realtime.on('doc_update', ...)`) is our only signal that
// frm.doc got replaced out from under an open dialog. The native grid
// redraws itself for free on every such refresh; this is how Expand View
// gets the same behavior.
let open_expand_view_state = {};

const AUTO_SYNC_SAVE_INTERVAL_MS = 2000;
const IDLE_CHECKPOINT_SAVE_MS = 8000;

// Module-scope so open_expand_view's dialog.on_hide can also trigger a
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
        EXPAND_VIEW_GRIDS.forEach(({fieldname, doctype}) => {
            frm.fields_dict[fieldname].grid.add_custom_button(__('Expand View'), () => {
                open_expand_view(frm, fieldname, doctype);
            });
        });

        Object.keys(open_expand_view_state).forEach(fieldname => {
            let state = open_expand_view_state[fieldname];
            if (state && state.dialog.display) {
                // Lightweight, not a rebuild - a full frm refresh fires
                // after every save (including our own auto-sync ones, every
                // ~2s during active editing - see form.js's after_save), so
                // destroying/recreating the DataTable here would interrupt
                // the user mid-edit for no reason. refresh_expand_view_data
                // still picks up genuinely external changes the same way.
                refresh_expand_view_data(frm, fieldname, state);
            }
        });
    }
});

function is_expand_view_field_editable(frm, docfield) {
    return !docfield.read_only
        && frm.perm[0].write
        && frm.doc.status !== 'Complete';
}

function get_expand_view_default_columns(child_doctype) {
    return frappe.meta.get_docfields(child_doctype)
        .filter(df => df.in_list_view)
        .map(df => df.fieldname);
}

function get_expand_view_rows(frm, fieldname) {
    // Always read straight from frm.doc, in its current order - the
    // dialog must never become a second source of truth for row order (FR-07).
    return (frm.doc[fieldname] || []).map(row => Object.assign({}, row));
}

function get_expand_view_columns(frm, child_doctype, selected_fieldnames) {
    return selected_fieldnames.map(fieldname => {
        let docfield = frappe.meta.get_docfield(child_doctype, fieldname) || {fieldname, label: fieldname};
        return {
            id: fieldname,
            name: __(docfield.label || fieldname),
            editable: is_expand_view_field_editable(frm, docfield),
            width: 140,
            format: (value) => frappe.format(value, docfield),
            docfield: docfield
        };
    });
}

function open_expand_view(frm, fieldname, child_doctype) {
    frappe.model.user_settings.get('Tender').then(settings => {
        let saved = ((settings || {}).ExpandView || {})[child_doctype];
        let state = {
            child_doctype,
            selected_fieldnames: (saved && saved.length) ? saved : get_expand_view_default_columns(child_doctype)
        };

        let dialog = new frappe.ui.Dialog({
            title: __('Expand View') + ': ' + __(child_doctype),
            size: 'extra-large',
            fields: [{fieldtype: 'HTML', fieldname: 'expand_view_html'}]
        });
        state.dialog = dialog;
        open_expand_view_state[fieldname] = state;
        dialog.on_hide = () => {
            if (open_expand_view_state[fieldname] === state) delete open_expand_view_state[fieldname];
            checkpoint_save(frm);
        };

        dialog.set_secondary_action_label(__('Columns'));
        dialog.set_secondary_action(() => {
            open_column_picker(frm, fieldname, child_doctype, state.selected_fieldnames, (new_selection) => {
                state.selected_fieldnames = new_selection;
                refresh_expand_view_data(frm, fieldname, state);
            });
        });

        if (fieldname === 'boq_items') {
            dialog.set_primary_action(__('Add Item from Template'), () => {
                dialog.hide();
                open_add_boq_item_dialog(frm, () => {
                    // add_boq_item_from_template's frm.reload_doc() replaces
                    // frm.doc wholesale, so this dialog's data/closures are
                    // stale by then anyway - reopening fresh (rather than
                    // trying to patch the old one back to life) is simplest,
                    // and lands the user back where they were per user request.
                    open_expand_view(frm, fieldname, child_doctype);
                });
            });
        }

        dialog.$wrapper.find('.modal-dialog').css('max-width', '95vw');
        // frappe.DataTable reads a live CSSStyleSheet off a <style> tag it
        // inserts into the wrapper - while the Bootstrap modal is still
        // mid fade-in, that tag's .sheet is null and construction throws.
        // on_page_show is Dialog's documented post-shown.bs.modal hook, so
        // by the time this runs the modal (and our wrapper) are fully laid out.
        dialog.on_page_show = () => {
            render_expand_view_datatable(frm, fieldname, state);
        };
        dialog.show();
    });
}

function render_expand_view_datatable(frm, fieldname, state) {
    let dialog = state.dialog;
    let child_doctype = state.child_doctype;

    // Full (re)build - only used for the dialog's first paint. Redraws
    // triggered afterward (edits, column-picker changes, a frm refresh
    // while the dialog stays open) go through refresh_expand_view_data
    // instead, which reuses this same DataTable instance rather than
    // tearing it down - see that function for why.
    if (dialog.expand_view_datatable) dialog.expand_view_datatable.destroy();

    let wrapper = dialog.fields_dict.expand_view_html.$wrapper;
    wrapper.empty();
    let $datatable_wrapper = $('<div class="expand-view-datatable">').appendTo(wrapper);

    let columns = get_expand_view_columns(frm, child_doctype, state.selected_fieldnames);
    dialog.expand_view_row_names = (frm.doc[fieldname] || []).map(row => row.name);

    let datatable = new frappe.DataTable($datatable_wrapper.get(0), {
        columns: columns,
        data: get_expand_view_rows(frm, fieldname),
        // 'fluid' (tried first) stretches/shrinks columns to fill the
        // container and explicitly forces overflow-x: hidden - the exact
        // opposite of what Expand View exists for (FR-02/FR-03: a wide,
        // horizontally-scrollable table once more columns are selected
        // than fit). 'fixed' keeps each column's own width and scrolls.
        layout: 'fixed',
        inlineFilters: true,
        headerDropdown: [],
        disableReorderColumn: true,
        dynamicRowHeight: true,
        serialNoColumn: true,
        getEditor(colIndex, rowIndex, value, parent, column) {
            if (!column.editable) return false;
            const $input = $('<input class="dt-input" type="text">').appendTo(parent);
            return {
                initValue(val) {
                    $input.val(val);
                    $input.trigger('focus');
                },
                getValue() {
                    return $input.val();
                },
                setValue(val) {
                    // Read live off the dialog, not a construction-time
                    // closure - refresh_expand_view_data updates this in
                    // place on every redraw without recreating the table.
                    let row_name = dialog.expand_view_row_names[rowIndex];
                    if (!row_name) return;
                    // getValue() always returns the raw text-input string -
                    // frappe.model.set_value does not coerce it, so a numeric
                    // fieldtype left as a string breaks downstream `+` math
                    // (tender.js's totals do plain string/number addition).
                    val = parse_expand_view_value(val, column.docfield.fieldtype);
                    return frappe.model.set_value(child_doctype, row_name, column.id, val).then(() => {
                        refresh_expand_view_data(frm, fieldname, state);
                    });
                }
            };
        }
    });

    dialog.expand_view_datatable = datatable;
    if (fieldname === 'boq_items') apply_expand_view_indent_styling(datatable, frm.doc[fieldname]);
}

function refresh_expand_view_data(frm, fieldname, state) {
    // The non-destructive counterpart to render_expand_view_datatable:
    // updates the existing DataTable's data/columns in place
    // (frappe.DataTable's own refresh(data, columns) method) instead of
    // destroying and recreating it. getEditor is bound once at construction
    // but reads its `column` argument live via datamanager.getColumn() per
    // edit, so column definitions (including editable/format) never go
    // stale just because we stopped rebuilding the table.
    let dialog = state.dialog;
    let datatable = dialog.expand_view_datatable;
    if (!datatable) return;

    let columns = get_expand_view_columns(frm, state.child_doctype, state.selected_fieldnames);
    dialog.expand_view_row_names = (frm.doc[fieldname] || []).map(row => row.name);
    datatable.refresh(get_expand_view_rows(frm, fieldname), columns);
    if (fieldname === 'boq_items') apply_expand_view_indent_styling(datatable, frm.doc[fieldname]);
}

function parse_expand_view_value(val, fieldtype) {
    if (['Int', 'Check'].includes(fieldtype)) return cint(val);
    if (['Float', 'Currency', 'Percent'].includes(fieldtype)) return flt(val);
    return val;
}

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

function open_column_picker(frm, fieldname, child_doctype, selected_fieldnames, on_apply) {
    let all_fields = frappe.meta.get_docfields(child_doctype)
        .filter(df => df.fieldname && !frappe.model.no_value_type.includes(df.fieldtype));

    let picker = new frappe.ui.Dialog({
        title: __('Columns') + ': ' + __(child_doctype),
        fields: [
            {
                fieldtype: 'MultiCheck',
                fieldname: 'columns',
                label: __('Visible Columns'),
                options: all_fields.map(df => ({
                    label: __(df.label || df.fieldname) + (df.hidden ? ' (' + __('hidden') + ')' : ''),
                    value: df.fieldname,
                    checked: selected_fieldnames.includes(df.fieldname)
                })),
                columns: 2
            }
        ],
        primary_action_label: __('Apply'),
        primary_action(values) {
            let new_selection = values.columns || [];
            if (!new_selection.length) {
                frappe.msgprint(__('Select at least one column.'));
                return;
            }
            frappe.model.user_settings.save('Tender', 'ExpandView', {[child_doctype]: new_selection}).then(() => {
                picker.hide();
                on_apply(new_selection);
            });
        }
    });
    picker.show();
}
