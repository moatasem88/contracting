frappe.ui.form.on("Purchase Order", {
    setup: function(frm) {
        frm.set_query("costing_note", function () {
			
			return {
				filters: {
					project : frm.doc.project
				
				},
			};
		});},
    costing_note: function(frm){
        if (frm.doc.costing_note){
        frappe.call({
            method: "contracting.utilis.get_costing_note_items",
            args: {
                doc: frm.doc,
            },
            callback: function(r) {
                if(r.message) {
                    frm.clear_table("items");
                    $.each(r.message, function(i, d) {
                        let row = frm.add_child("items");
                        console.log(d)
                        $.extend(row, d);
                    });
                    frm.refresh_field("items");
                }
            }
        });
    }}
});
