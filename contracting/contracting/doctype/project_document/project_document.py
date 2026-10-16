import os
import re
import shutil

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

ARCHIVE_BASE_PATH = "/home/frappe/my_archive/hard_links"
RESPONSE_CODES = ("A", "B", "C", "D")


def _clean(text):
    return re.sub(r'[<>:"/\\|?*]', '', str(text)).strip()


class ProjectDocument(Document):

    def autoname(self):
        if not (self.attachment_type and self.project and self.department and self.date):
            frappe.throw(_("Please set Attachment Type, Project, Department and Date before saving."))

        year = str(getdate(self.date).year)
        work_item_code = self.get_work_item_code()

        if self.attachment_version and self.parent_attachment:
            self.validate_parent_for_version()
            root_name, root_base_counter = self.get_root_and_base_counter()
            self.root_attachment = root_name

            version_count = frappe.db.count("Project Document", {"root_attachment": root_name}) + 1
            version_idx = str(version_count).zfill(3)

            self.name = (
                f"R-{year}-{self.project}-{self.department}-{self.attachment_type}"
                f"-{work_item_code}-{root_base_counter}-V{version_idx}"
            )
        else:
            self.root_attachment = None

            count_proj = frappe.db.count("Project Document", {
                "project": self.project,
                "attachment_type": self.attachment_type,
                "attachment_version": 0,
            })
            counter = str(count_proj + 1).zfill(3)
            self.base_counter = counter

            self.name = (
                f"FR-{year}-{self.project}-{self.department}-{self.attachment_type}"
                f"-{work_item_code}-{counter}"
            )

    def get_work_item_code(self):
        if not self.work_item:
            frappe.throw(_("Please select Work Item before saving."))

        code = frappe.db.get_value("work_item", self.work_item, "كود_بند_الاعمال")
        if not code:
            frappe.throw(_("The selected Work Item {0} has no code configured.").format(self.work_item))
        return code

    def validate_parent_for_version(self):
        parent = frappe.db.get_value(
            "Project Document", self.parent_attachment, ["code", "date"], as_dict=True
        ) or frappe._dict()
        if parent.code not in ("C", "D"):
            frappe.throw(
                _("A new version can only be created from an attachment with Code C or D. {0} has code {1}.")
                .format(self.parent_attachment, parent.code or _("not set"))
            )

        if self.date and parent.date and getdate(self.date) < getdate(parent.date):
            frappe.throw(
                _("Version Date ({0}) cannot be older than the Date ({1}) of Parent Attachment {2}.")
                .format(self.date, parent.date, self.parent_attachment)
            )

        has_ab_version = frappe.db.exists("Project Document", {
            "parent_attachment": self.parent_attachment,
            "code": ["in", ["A", "B"]],
            "name": ["!=", self.name or ""],
        })
        if has_ab_version:
            frappe.throw(
                _("{0} already has an approved/rejected version and cannot be used as a Parent Attachment.")
                .format(self.parent_attachment)
            )

    def get_root_and_base_counter(self):
        parent = frappe.db.get_value(
            "Project Document", self.parent_attachment,
            ["root_attachment", "base_counter"], as_dict=True
        )
        if not parent:
            frappe.throw(_("Parent Attachment {0} not found.").format(self.parent_attachment))

        if parent.root_attachment:
            root_name = parent.root_attachment
            base_counter = frappe.db.get_value("Project Document", root_name, "base_counter")
        else:
            root_name = self.parent_attachment
            base_counter = parent.base_counter

        return root_name, base_counter

    def validate(self):
        if self.attachment_version and not self.parent_attachment:
            frappe.throw(_("Please select a Parent Attachment for this version."))
        if not self.attachment_version:
            self.parent_attachment = None

    def before_save(self):
        self.archive_attachments()
        self.clear_transient_history()

    def before_update_after_submit(self):
        # Frappe routes saves on an already-submitted document through a *separate* lifecycle:
        # before_update_after_submit / on_update_after_submit, not before_save / on_update. Since
        # the Consultant Response (code, response_attachment, ...) is only ever set after the base
        # attachment has been submitted, it always goes through this path -- before_save is never
        # called for it, so the archiving/sync logic must be hooked here too.
        self.archive_attachments()
        self.clear_transient_history()

    def clear_transient_history(self):
        # "history" is populated fresh on every onload() purely for display (see sync_history) and
        # is never meant to be persisted as real rows -- it's a computed view over the version
        # family, rebuilt from scratch each time. But once a document has been loaded in the Desk
        # UI, the client round-trips whatever the grid currently shows back to the server on the
        # next save, and each of those rows carries a `name` from that earlier synthetic append().
        # Left alone, Frappe's update_children() tries to treat them as existing rows to update,
        # and on a later save fails with "Project Document History <name> not found" once Frappe
        # re-loads the document to validate an update-after-submit change and finds those rows
        # were never actually written to tabProject Document History. Clearing the table right
        # before every persist -- exactly like previous_invoices/revisions are cleared in
        # Subcontractor invoice Control's before_save -- keeps it purely transient.
        self.set("history", [])

    def archive_attachments(self):
        if self.attachment and self.has_value_changed("attachment"):
            self.attachment = self.archive_file(self.attachment, "", "attachment")
        if self.response_attachment and self.has_value_changed("response_attachment"):
            self.response_attachment = self.archive_file(self.response_attachment, "-CR", "response_attachment")

    def onload(self):
        if not self.is_new():
            self.sync_history()

    def on_update(self):
        self.after_save_sync()

    def on_update_after_submit(self):
        self.after_save_sync()

    def after_save_sync(self):
        self.sync_history()
        self.clean_duplicate_file_records()

    def clean_duplicate_file_records(self):
        """Safety net: remove any duplicate File rows left over for this document (same
        file_url attached more than once), keeping only the earliest one."""
        try:
            frappe.db.sql("""
                DELETE FROM `tabFile`
                WHERE attached_to_doctype = %s
                  AND attached_to_name = %s
                  AND name NOT IN (
                      SELECT min_name FROM (
                          SELECT MIN(name) as min_name
                          FROM `tabFile`
                          WHERE attached_to_doctype = %s AND attached_to_name = %s
                          GROUP BY file_url
                      ) as tmp
                  )
            """, (self.doctype, self.name, self.doctype, self.name))
            frappe.clear_document_cache(self.doctype, self.name)
        except Exception:
            frappe.log_error(title="Project Document File Cleanup Error", message=frappe.get_traceback())

    @frappe.whitelist()
    def sync_history(self):
        self.set("history", [])
        if self.is_new():
            return []

        root_name = self.root_attachment or self.name
        family = frappe.db.get_all(
            "Project Document",
            filters={"name": ["in", self._family_names(root_name)]},
            fields=[
                "name", "attachment_version", "parent_attachment", "attachment_type",
                "date", "attachment", "description", "zone", "code", "reply_date",
                "approved_by", "consultant_office", "response_attachment",
            ],
            order_by="creation asc",
        )

        for row in family:
            self.append("history", {
                "attachment_name": row.name,
                "is_version": row.attachment_version,
                "parent_attachment": row.parent_attachment,
                "attachment_type": row.attachment_type,
                "date": row.date,
                "attachment": row.attachment,
                "description": row.description,
                "zone": row.zone,
                "code": row.code,
                "reply_date": row.reply_date,
                "approved_by": row.approved_by,
                "consultant_office": row.consultant_office,
                "response_attachment": row.response_attachment,
            })

        return family

    def _family_names(self, root_name):
        versions = frappe.db.get_all("Project Document", {"root_attachment": root_name}, "name")
        names = {root_name, self.name}
        names.update(v.name for v in versions)
        return list(names)

    def archive_file(self, file_url, suffix, fieldname):
        """Copy an uploaded attachment into the external project archive and rename it to the
        document's serial number, mirroring contract_archiving.archive_logic exactly."""
        if not file_url:
            return file_url

        try:
            file_url = "/" + file_url.lstrip("/")
            ext = os.path.splitext(file_url)[1]
            final_name = f"{self.name}{suffix}{ext}"
            expected_private_url = f"/private/files/{final_name}"

            if file_url == expected_private_url:
                return file_url

            source_path = (
                frappe.get_site_path(file_url.lstrip("/"))
                if file_url.startswith("/private")
                else frappe.get_site_path("public", file_url.lstrip("/"))
            )
            if not os.path.exists(source_path):
                return file_url

            target_dir = os.path.join(ARCHIVE_BASE_PATH, _clean(self.project), _clean(self.attachment_type))
            os.makedirs(target_dir, mode=0o777, exist_ok=True)
            target_path = os.path.join(target_dir, final_name)

            private_dest_path = frappe.get_site_path("private", "files", final_name)
            os.makedirs(os.path.dirname(private_dest_path), mode=0o777, exist_ok=True)

            shutil.copy2(source_path, target_path)
            shutil.copy2(source_path, private_dest_path)

            # Find the File record to rename in place, rather than inserting a new one for the
            # archived copy. This matters for two reasons:
            #
            # 1. Ownership safety: file_url alone is not a safe match. Frappe deduplicates File
            #    storage by content hash -- if this document's base attachment and its later
            #    Consultant Response attachment happen to have identical byte content, Frappe
            #    silently points the second upload's file_url at the first file's URL. Picking
            #    "any" File row with that file_url would then grab the base attachment's own File
            #    record while archiving the response attachment, hijacking it away from the
            #    "attachment" field (and deleting its physical file below) -- which is exactly what
            #    caused the attachment link to start returning Forbidden. So we only ever take over
            #    a row that is either already tagged for this exact (attached_to_doctype,
            #    attached_to_name, attached_to_field) -- a re-save -- or not genuinely claimed by
            #    any *real, saved* document -- the original, not-yet-archived upload -- never one
            #    already claimed by a different field/document. "Not genuinely claimed" has to mean
            #    more than "attached_to_name is empty": when a file is attached to a doctype's
            #    Attach field before the document is first saved, Frappe's client already sets
            #    attached_to_doctype/attached_to_field, but attached_to_name is still the browser's
            #    temporary "new-<doctype>-xxxxx" placeholder, which will never correspond to an
            #    actual saved document -- so check DB existence of that name rather than just
            #    whether the column happens to be populated.
            #
            # 2. Avoiding Frappe's insert-time dedup entirely: File.before_insert() unconditionally
            #    runs its own content-hash duplicate check (inside the instance's save_file()
            #    method, independent of validate_duplicate_entry and not suppressible via any flag).
            #    Since we just copied the exact same bytes to the new archived path, a brand-new
            #    insert will always find the original upload's File row as a "duplicate" and get its
            #    file_url silently redirected right back to that original, un-archived path -- so
            #    the rename never actually sticks. Updating the *existing* row instead avoids
            #    before_insert() altogether (that dedup check never runs on a plain .save() update),
            #    so the clean, archived name reliably sticks.
            candidates = frappe.get_all("File", filters={"file_url": file_url}, fields=[
                "name", "attached_to_doctype", "attached_to_name", "attached_to_field",
            ])

            existing_file_name = None
            for candidate in candidates:
                if (
                    candidate.attached_to_doctype == self.doctype
                    and candidate.attached_to_name == self.name
                    and candidate.attached_to_field == fieldname
                ):
                    existing_file_name = candidate.name
                    break
            else:
                for candidate in candidates:
                    belongs_to_another_real_doc = (
                        candidate.attached_to_name
                        and candidate.attached_to_name != self.name
                        and frappe.db.exists(candidate.attached_to_doctype or self.doctype, candidate.attached_to_name)
                    )
                    if not belongs_to_another_real_doc:
                        existing_file_name = candidate.name
                        break

            if existing_file_name:
                file_doc = frappe.get_doc("File", existing_file_name)
                file_doc.file_url = expected_private_url
                file_doc.file_name = final_name
                file_doc.is_private = 1
                file_doc.attached_to_name = self.name
                file_doc.attached_to_doctype = self.doctype
                file_doc.attached_to_field = fieldname
                file_doc.save(ignore_permissions=True)
            else:
                # Rare fallback (no matching File row found at all, e.g. a manually-typed URL):
                # this does go through before_insert()'s dedup check, so the resulting file_url may
                # get redirected to an existing duplicate's path -- always trust whatever it actually
                # ends up as rather than assuming our requested clean name stuck.
                file_doc = frappe.get_doc({
                    "doctype": "File",
                    "file_url": expected_private_url,
                    "file_name": final_name,
                    "is_private": 1,
                    "attached_to_doctype": self.doctype,
                    "attached_to_name": self.name,
                    "attached_to_field": fieldname,
                })
                file_doc.insert(ignore_permissions=True)

            expected_private_url = file_doc.file_url
            self.remove_stale_field_files(fieldname, file_doc.name, expected_private_url, file_doc.content_hash)

            # Only remove the old physical copy if no other File row still points at it -- with
            # content-hash dedup, some other document's attachment may legitimately still need it.
            still_referenced = frappe.db.exists("File", {"file_url": file_url, "name": ["!=", file_doc.name]})
            if not still_referenced and source_path != private_dest_path and os.path.exists(source_path):
                try:
                    os.remove(source_path)
                except OSError:
                    pass

            return expected_private_url
        except Exception:
            frappe.log_error(title="Project Document Archive Error", message=frappe.get_traceback())
            return file_url

    def remove_stale_field_files(self, fieldname, keep_file_name, keep_file_url, keep_content_hash):
        """An Attach field can only ever hold one file, but the lookup above only ever matches
        a File row sharing the file_url of the *incoming* upload. When a replacement upload has
        a different extension (or otherwise hashes to different content), its target filename
        never collides with the previously-archived file, so that old File row -- still tagged
        attached_to_doctype/name/field for this exact document and field -- is never revisited or
        touched. It keeps existing after the "replacement", still archived on disk, and keeps
        showing up in the Desk 'Attachments' sidebar even though self.<fieldname> now correctly
        points at the new file with the same serial number. Explicitly find and remove any other
        File row still claiming this document's field slot."""
        stale_files = frappe.get_all("File", filters={
            "attached_to_doctype": self.doctype,
            "attached_to_name": self.name,
            "attached_to_field": fieldname,
            "name": ["!=", keep_file_name],
        }, fields=["name", "file_url", "content_hash"])

        for stale in stale_files:
            try:
                hard_link_path = None
                if stale.file_url and stale.file_url == keep_file_url:
                    # Same target filename as the surviving row (a same-extension replace, which
                    # reuses the archived path so the serial number's file just gets overwritten
                    # in place). File.on_trash -> _delete_file_on_disk decides whether to remove
                    # the physical copy by matching content_hash against *other* File rows, not
                    # by file_url -- it has no idea these two rows share a path. This stale row's
                    # content_hash is still whatever was recorded for the OLD content it was
                    # originally saved with, so left alone it won't be recognized as "shared"
                    # with the surviving row and Frappe will delete the physical file both rows
                    # point at, destroying the just-written replacement along with it. Align the
                    # stale row's content_hash with the surviving row's before deleting so
                    # Frappe's own dedup check correctly treats the copy as shared and leaves it.
                    if keep_content_hash and stale.content_hash != keep_content_hash:
                        frappe.db.set_value("File", stale.name, "content_hash", keep_content_hash, update_modified=False)
                elif stale.file_url:
                    still_referenced_elsewhere = frappe.db.exists("File", {
                        "file_url": stale.file_url,
                        "name": ["not in", [stale.name, keep_file_name]],
                    })
                    if not still_referenced_elsewhere:
                        hard_link_path = os.path.join(
                            ARCHIVE_BASE_PATH, _clean(self.project), _clean(self.attachment_type),
                            os.path.basename(stale.file_url),
                        )

                # Removes the File row and its private/files copy (frappe.delete_doc ->
                # File.on_trash -> _delete_file_on_disk). The parallel hard_links archive copy is
                # outside Frappe's storage, so it needs its own cleanup.
                frappe.delete_doc("File", stale.name, ignore_permissions=True, delete_permanently=True)

                if hard_link_path and os.path.exists(hard_link_path):
                    os.remove(hard_link_path)
            except Exception:
                frappe.log_error(title="Project Document Stale File Cleanup Error", message=frappe.get_traceback())


@frappe.whitelist()
def get_parent_attachment_options(doctype, txt, searchfield, start, page_len, filters):
    """Link query for the Parent Attachment field: only C/D-coded attachments of the same
    project + attachment type (and work item, when set) that have not already been superseded
    by an A/B-coded version."""
    filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
    project = filters.get("project")
    attachment_type = filters.get("attachment_type")
    work_item = filters.get("work_item")

    if not (project and attachment_type):
        return []

    query_filters = {
        "project": project,
        "attachment_type": attachment_type,
        "code": ["in", ["C", "D"]],
        "name": ["like", f"%{txt}%"],
    }
    if work_item:
        query_filters["work_item"] = work_item

    candidates = frappe.db.get_all(
        "Project Document", filters=query_filters, fields=["name"], order_by="creation desc"
    )

    results = []
    for candidate in candidates:
        already_superseded = frappe.db.exists("Project Document", {
            "parent_attachment": candidate.name,
            "code": ["in", ["A", "B"]],
        })
        if not already_superseded:
            results.append([candidate.name])

    return results[start:start + page_len]


@frappe.whitelist()
def submit_consultant_response(name, reply_date, approved_by, code, consultant_office, response_attachment=None):
    """Save the Consultant Response fields as one atomic update, called from the
    'Consultant Response' dialog. Keeping this server-side (rather than letting the client set
    the fields directly) guarantees `code` is never saved without the rest of the response."""
    if code not in RESPONSE_CODES:
        frappe.throw(_("Invalid Code. Must be one of A, B, C, D."))
    if not (reply_date and approved_by and consultant_office):
        frappe.throw(_("Reply Date, Approved By and Consultant Office are required."))

    doc = frappe.get_doc("Project Document", name)
    doc.check_permission("write")

    if doc.docstatus != 1:
        frappe.throw(_("A Consultant Response can only be recorded after the attachment has been submitted."))

    if doc.code:
        frappe.throw(
            _("A Consultant Response has already been recorded for {0} and cannot be changed.")
            .format(name)
        )

    if doc.date and getdate(reply_date) < getdate(doc.date):
        frappe.throw(
            _("Reply Date ({0}) cannot be earlier than the Date ({1}) of this attachment.")
            .format(reply_date, doc.date)
        )

    doc.reply_date = reply_date
    doc.approved_by = approved_by
    doc.code = code
    doc.consultant_office = consultant_office
    if response_attachment:
        doc.response_attachment = response_attachment

    doc.save()
    return doc.as_dict()
