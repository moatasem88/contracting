// Copyright (c) 2026, moatasem and contributors
// For license information, please see license.txt

frappe.ui.form.on('Project Document', {
	onload: function(frm) {
		frm.trigger('setup_parent_attachment_filter');
	},

	refresh: function(frm) {
		frm.clear_custom_buttons();

		frm.trigger('setup_parent_attachment_filter');
		frm.trigger('toggle_parent_attachment');
		frm.trigger('secure_history_grid');

		if (!frm.is_new()) {
			if (frm.doc.docstatus === 1) {
				frm.add_custom_button(__('Consultant Response'), function() {
					frm.trigger('open_consultant_response_dialog');
				}).addClass('btn-primary');
			}

			frm.trigger('fetch_history_instantly');
		}
	},

	fetch_history_instantly: function(frm) {
		frm.call('sync_history').then(() => {
			frm.refresh_field('history');
		});
	},

	attachment_type: function(frm) {
		frm.trigger('setup_parent_attachment_filter');
	},

	project: function(frm) {
		frm.trigger('setup_parent_attachment_filter');
	},

	work_item: function(frm) {
		frm.trigger('setup_parent_attachment_filter');
	},

	attachment_version: function(frm) {
		frm.trigger('toggle_parent_attachment');
	},

	parent_attachment: function(frm) {
		if (!frm.doc.parent_attachment) return;

		frappe.db.get_value('Project Document', frm.doc.parent_attachment,
			['project', 'work_item', 'department', 'attachment_type']).then(r => {
			if (!r || !r.message) return;
			const parent = r.message;
			if (parent.project) frm.set_value('project', parent.project);
			if (parent.work_item) frm.set_value('work_item', parent.work_item);
			if (parent.department) frm.set_value('department', parent.department);
			if (parent.attachment_type) frm.set_value('attachment_type', parent.attachment_type);
		});
	},

	toggle_parent_attachment: function(frm) {
		frm.toggle_reqd('parent_attachment', frm.doc.attachment_version);
	},

	secure_history_grid: function(frm) {
		if (frm.fields_dict['history']) {
			frm.get_field('history').grid.cannot_add_rows = true;
			frm.get_field('history').grid.cannot_delete_rows = true;
		}
	},

	setup_parent_attachment_filter: function(frm) {
		frm.set_query('parent_attachment', function() {
			return {
				query: 'contracting.contracting.doctype.project_document.project_document.get_parent_attachment_options',
				filters: {
					project: frm.doc.project,
					attachment_type: frm.doc.attachment_type,
					work_item: frm.doc.work_item,
				}
			};
		});
	},

	open_consultant_response_dialog: function(frm) {
		// Once a response has been recorded (code is set), the server refuses to change it --
		// submit_consultant_response is the only path that ever writes these fields, and it
		// throws if doc.code is already set. Mirror that lock here: show the recorded values
		// read-only instead of letting the user fill the form out again and hit that error only
		// after clicking Update.
		const is_locked = !!frm.doc.code;

		const dialog = new frappe.ui.Dialog({
			title: __('Consultant Response'),
			fields: [
				{
					fieldname: 'reply_date',
					fieldtype: 'Date',
					label: __('Reply Date'),
					reqd: 1,
					read_only: is_locked,
					default: frm.doc.reply_date || frappe.datetime.get_today(),
				},
				{
					fieldname: 'approved_by',
					fieldtype: 'Data',
					label: __('Approved By'),
					reqd: 1,
					read_only: is_locked,
					default: frm.doc.approved_by,
				},
				{
					fieldname: 'code',
					fieldtype: 'Select',
					label: __('Code'),
					options: ['A', 'B', 'C', 'D'],
					reqd: 1,
					read_only: is_locked,
					default: frm.doc.code,
				},
				{
					fieldname: 'consultant_office',
					fieldtype: 'Link',
					label: __('Consultant Office'),
					options: 'Engineering consulting office',
					reqd: 1,
					read_only: is_locked,
					default: frm.doc.consultant_office,
				},
				{
					fieldname: 'response_attachment',
					fieldtype: 'Attach',
					label: __('Response Attachment'),
					reqd: 1,
					read_only: is_locked,
					default: frm.doc.response_attachment,
				},
			],
			primary_action_label: is_locked ? undefined : __('Update'),
			primary_action: is_locked ? undefined : function(values) {
				dialog.get_primary_btn().attr('disabled', true);
				frappe.call({
					method: 'contracting.contracting.doctype.project_document.project_document.submit_consultant_response',
					args: {
						name: frm.doc.name,
						reply_date: values.reply_date,
						approved_by: values.approved_by,
						code: values.code,
						consultant_office: values.consultant_office,
						response_attachment: values.response_attachment,
					},
					callback: function() {
						dialog.hide();
						frm.reload_doc();
					},
					always: function() {
						dialog.get_primary_btn().attr('disabled', false);
					}
				});
			}
		});

		if (is_locked) {
			dialog.get_primary_btn().hide();
		}

		dialog.show();
	},
});
