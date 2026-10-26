frappe.ui.form.on("Mailgun Domain", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Test connection"), () => call_and_show(frm, "test_connection"));
		frm.add_custom_button(__("Register webhooks"), () => call_and_show(frm, "register_webhooks"));
	},
});

function call_and_show(frm, method) {
	frappe.call({
		doc: frm.doc,
		method: method,
		freeze: true,
		callback(r) {
			if (!r.message) return;
			frappe.msgprint({ title: __(method === "test_connection" ? "Test connection" : "Register webhooks"), message: definition_list(r.message), wide: true });
			frm.reload_doc();
		},
	});
}

function definition_list(value) {
	const rows = Object.entries(value).map(([key, item]) => {
		const text = Array.isArray(item) ? item.join(", ") : item !== null && typeof item === "object" ? definition_list(item) : String(item);
		return `<dt>${frappe.utils.escape_html(key)}</dt><dd>${typeof item === "object" && item !== null && !Array.isArray(item) ? text : frappe.utils.escape_html(text)}</dd>`;
	});
	return `<dl>${rows.join("")}</dl>`;
}
