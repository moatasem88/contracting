// Shared Expand View: a wide, horizontally scrollable, inline-editable
// dialog over one child table of a form. Extracted from tender_expand_view.js
// (2026-10-18 BRD, FR-30) so Subcontractor Contract, Contractor Invoice and
// Contractor Contract Addendum can offer the same thing Tender does.
//
// Usage, from a doctype's own refresh():
//
//     contracting.expand_view.attach(frm, [{fieldname: 'contracted_items', doctype: 'Contractor Contract Item'}]);
//
// Tender passes `options` for the behaviour only it has (checkpoint save on
// close, "Add Item from Template", indented BOQ names, a Complete-status
// lock, and free-text editing of Link/Select cells). Everything else
// defaults to the stricter behaviour the contract/invoice/addendum forms
// need: Link and Select cells are read-only (the dialog's editor is a plain
// text input, not a picker - edit those in the native grid), cells follow
// the grid's own read-only state and each field's read_only_depends_on, and
// columns the user cannot read (permlevel > 0 without read) are never listed.
//
// options = {
//     is_editable(frm, docfield, fieldname)   replaces the default editability test
//     allow_link_edit                         true: Link/Select cells stay editable (Tender)
//     on_hide(frm)                            runs when the dialog closes
//     primary_action(frm, fieldname, child_doctype, ctx)   -> {label, handler} or null
//     after_render(datatable, rows, fieldname)             post-paint styling hook
// }

frappe.provide('contracting.expand_view');

(function () {
    // Open dialogs, keyed by "<doctype>:<grid fieldname>". frm's own refresh()
    // (which also fires for the realtime doc_update auto-reload) is the only
    // signal that frm.doc got replaced under an open dialog; the native grid
    // redraws itself for free on every such refresh and this is how Expand
    // View gets the same behaviour.
    const open_state = {};
    const NON_VALUE_TYPES = ['Table', 'Table MultiSelect', 'Button', 'HTML', 'Image', 'Attach', 'Attach Image', 'Signature'];
    const PICKER_TYPES = ['Link', 'Dynamic Link', 'Select'];

    const state_key = (frm, fieldname) => frm.doctype + ':' + fieldname;

    function can_read_field(frm, docfield) {
        const perm = (frm.perm || [])[docfield.permlevel || 0];
        return !!(perm && perm.read);
    }

    function visible_docfields(frm, child_doctype) {
        return frappe.meta.get_docfields(child_doctype)
            .filter(df => df.fieldname && !frappe.model.no_value_type.includes(df.fieldtype) && can_read_field(frm, df));
    }

    function default_columns(frm, child_doctype) {
        return visible_docfields(frm, child_doctype).filter(df => df.in_list_view).map(df => df.fieldname);
    }

    function column_editable(frm, fieldname, docfield, options) {
        if (options.is_editable) return options.is_editable(frm, docfield, fieldname);
        if (docfield.read_only || NON_VALUE_TYPES.includes(docfield.fieldtype)) return false;
        if (!options.allow_link_edit && PICKER_TYPES.includes(docfield.fieldtype)) return false;
        const grid_field = frm.fields_dict[fieldname];
        return !!(grid_field && grid_field.grid && grid_field.grid.is_editable());
    }

    // read_only_depends_on is per row (it can read the row's own fields), so
    // it is checked when a cell is opened for editing rather than once per column.
    function cell_read_only(frm, docfield, row) {
        const expr = docfield.read_only_depends_on;
        if (!expr || typeof expr !== 'string') return false;
        const code = expr.startsWith('eval:') ? expr.substr(5) : 'doc.' + expr;
        try {
            return !!frappe.utils.eval(code, {doc: row, parent: frm.doc});
        } catch (e) {
            return false;
        }
    }

    function get_rows(frm, fieldname) {
        // Always read straight from frm.doc, in its current order - the
        // dialog must never become a second source of truth for row order.
        return (frm.doc[fieldname] || []).map(row => Object.assign({}, row));
    }

    function get_columns(frm, fieldname, child_doctype, selected, options) {
        return selected
            .map(id => frappe.meta.get_docfield(child_doctype, id))
            .filter(docfield => docfield && can_read_field(frm, docfield))
            .map(docfield => ({
                id: docfield.fieldname,
                name: __(docfield.label || docfield.fieldname),
                editable: column_editable(frm, fieldname, docfield, options),
                width: 140,
                format: (value) => frappe.format(value, docfield),
                docfield: docfield
            }));
    }

    function parse_value(val, fieldtype) {
        if (['Int', 'Check'].includes(fieldtype)) return cint(val);
        if (['Float', 'Currency', 'Percent'].includes(fieldtype)) return flt(val);
        return val;
    }

    function refresh_data(frm, fieldname, state) {
        // Non-destructive: updates the existing DataTable in place instead of
        // rebuilding it, so an edit (or a frm refresh while the dialog stays
        // open) never interrupts the user mid-cell.
        const dialog = state.dialog;
        const datatable = dialog.expand_view_datatable;
        if (!datatable) return;

        const columns = get_columns(frm, fieldname, state.child_doctype, state.selected_fieldnames, state.options);
        dialog.expand_view_row_names = (frm.doc[fieldname] || []).map(row => row.name);
        datatable.refresh(get_rows(frm, fieldname), columns);
        if (state.options.after_render) state.options.after_render(datatable, frm.doc[fieldname], fieldname);
    }

    function render_datatable(frm, fieldname, state) {
        const dialog = state.dialog;
        const child_doctype = state.child_doctype;

        if (dialog.expand_view_datatable) dialog.expand_view_datatable.destroy();

        const wrapper = dialog.fields_dict.expand_view_html.$wrapper;
        wrapper.empty();
        const $datatable_wrapper = $('<div class="expand-view-datatable">').appendTo(wrapper);

        const columns = get_columns(frm, fieldname, child_doctype, state.selected_fieldnames, state.options);
        dialog.expand_view_row_names = (frm.doc[fieldname] || []).map(row => row.name);

        const datatable = new frappe.DataTable($datatable_wrapper.get(0), {
            columns: columns,
            data: get_rows(frm, fieldname),
            // 'fluid' forces overflow-x: hidden - the opposite of what this
            // view exists for. 'fixed' keeps each column's width and scrolls.
            layout: 'fixed',
            inlineFilters: true,
            headerDropdown: [],
            disableReorderColumn: true,
            dynamicRowHeight: true,
            serialNoColumn: true,
            getEditor(colIndex, rowIndex, value, parent, column) {
                if (!column.editable) return false;
                const row_name = dialog.expand_view_row_names[rowIndex];
                const row = row_name && locals[child_doctype] && locals[child_doctype][row_name];
                if (row && cell_read_only(frm, column.docfield, row)) return false;

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
                        // Read live off the dialog, not a construction-time closure.
                        const name = dialog.expand_view_row_names[rowIndex];
                        if (!name) return;
                        // getValue() is always the raw text; frappe.model.set_value
                        // does not coerce it and downstream math does plain `+`.
                        val = parse_value(val, column.docfield.fieldtype);
                        return frappe.model.set_value(child_doctype, name, column.id, val).then(() => {
                            refresh_data(frm, fieldname, state);
                        });
                    }
                };
            }
        });

        dialog.expand_view_datatable = datatable;
        if (state.options.after_render) state.options.after_render(datatable, frm.doc[fieldname], fieldname);
    }

    function open_column_picker(frm, child_doctype, selected, on_apply) {
        const all_fields = visible_docfields(frm, child_doctype);

        const picker = new frappe.ui.Dialog({
            title: __('Columns') + ': ' + __(child_doctype),
            fields: [
                {
                    fieldtype: 'MultiCheck',
                    fieldname: 'columns',
                    label: __('Visible Columns'),
                    options: all_fields.map(df => ({
                        label: __(df.label || df.fieldname) + (df.hidden ? ' (' + __('hidden') + ')' : ''),
                        value: df.fieldname,
                        checked: selected.includes(df.fieldname)
                    })),
                    columns: 2
                }
            ],
            primary_action_label: __('Apply'),
            primary_action(values) {
                const new_selection = values.columns || [];
                if (!new_selection.length) {
                    frappe.msgprint(__('Select at least one column.'));
                    return;
                }
                // Keyed by the form's own doctype, so Tender's saved choices
                // and those of the other forms never collide.
                frappe.model.user_settings.save(frm.doctype, 'ExpandView', {[child_doctype]: new_selection}).then(() => {
                    picker.hide();
                    on_apply(new_selection);
                });
            }
        });
        picker.show();
    }

    function open(frm, fieldname, child_doctype, options) {
        frappe.model.user_settings.get(frm.doctype).then(settings => {
            const saved = ((settings || {}).ExpandView || {})[child_doctype];
            const allowed = new Set(visible_docfields(frm, child_doctype).map(df => df.fieldname));
            const chosen = (saved && saved.length ? saved : default_columns(frm, child_doctype)).filter(id => allowed.has(id));
            const state = {
                child_doctype,
                options,
                selected_fieldnames: chosen.length ? chosen : default_columns(frm, child_doctype)
            };

            const dialog = new frappe.ui.Dialog({
                title: __('Expand View') + ': ' + __(child_doctype),
                size: 'extra-large',
                fields: [{fieldtype: 'HTML', fieldname: 'expand_view_html'}]
            });
            state.dialog = dialog;
            const key = state_key(frm, fieldname);
            open_state[key] = state;
            dialog.on_hide = () => {
                if (open_state[key] === state) delete open_state[key];
                if (options.on_hide) options.on_hide(frm);
            };

            dialog.set_secondary_action_label(__('Columns'));
            dialog.set_secondary_action(() => {
                open_column_picker(frm, child_doctype, state.selected_fieldnames, (new_selection) => {
                    state.selected_fieldnames = new_selection;
                    refresh_data(frm, fieldname, state);
                });
            });

            const primary = options.primary_action && options.primary_action(frm, fieldname, child_doctype, {
                reopen: () => open(frm, fieldname, child_doctype, options),
                dialog
            });
            if (primary) {
                dialog.set_primary_action(primary.label, () => {
                    dialog.hide();
                    primary.handler();
                });
            }

            dialog.$wrapper.find('.modal-dialog').css('max-width', '95vw');
            // frappe.DataTable reads a live CSSStyleSheet off a <style> tag it
            // inserts into the wrapper - mid fade-in that sheet is null and
            // construction throws. on_page_show fires after shown.bs.modal.
            dialog.on_page_show = () => {
                render_datatable(frm, fieldname, state);
            };
            dialog.show();
        });
    }

    contracting.expand_view.attach = function (frm, grids, options) {
        options = options || {};
        grids.forEach(({fieldname, doctype}) => {
            const field = frm.fields_dict[fieldname];
            if (!field || !field.grid) return;
            field.grid.add_custom_button(__('Expand View'), () => open(frm, fieldname, doctype, options));
        });
        contracting.expand_view.refresh_open(frm);
    };

    // For callers with their own entry point (e.g. the invoice tree view).
    contracting.expand_view.open_grid = function (frm, fieldname, doctype, options) {
        open(frm, fieldname, doctype, options || {});
    };

    // Lightweight, not a rebuild - a frm refresh fires after every save, and
    // destroying/recreating the DataTable would interrupt the user mid-edit.
    contracting.expand_view.refresh_open = function (frm) {
        Object.keys(open_state).forEach(key => {
            const [doctype, fieldname] = key.split(':');
            const state = open_state[key];
            if (doctype === frm.doctype && state && state.dialog.display) {
                refresh_data(frm, fieldname, state);
            }
        });
    };
})();
