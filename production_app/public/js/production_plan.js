// FU62 — Wizard "Tambah Item" untuk form Production Plan (Assembly Items /
// po_items). Dipindah dari Client Script `ProductionPlanModal` (4 langkah)
// menjadi JS bundel app via hooks.doctype_js: 2 langkah, Bahasa Indonesia,
// dan bisa input beberapa item dalam satu sesi.
//
// Kontrak penting (diverifikasi di source native v16):
// - 1 baris po_items = 1 Work Order (production_plan.py loop create_work_order
//   per baris) — memecah N baris memang cara membuat N Work Order.
// - Production Plan Item TIDAK punya field `uom`/`item_name`; hanya
//   item_code, bom_no, planned_qty, stock_uom yang ditulis.
// - Matematika alokasi SELALU di stock UOM; baris terakhir menampung sisa
//   pembagian supaya Σ baris == total persis. Kolom UOM input di preview
//   hanyalah tampilan turunan (≈), tidak pernah ikut dihitung.

(function () {
	const API = "production_app.api.production_plan";
	const MODE_PER_WO = "per_wo";
	const MODE_TOTAL = "total";
	const MODE_STORAGE_KEY = "production_app.pp_wizard.last_mode";
	const PREVIEW_ROWS = 5;
	const WO_SOFT_LIMIT = 50;

	frappe.ui.form.on("Production Plan", {
		refresh(frm) {
			if (frm.doc.docstatus !== 0 || frm.doc.status === "Closed") return;
			frm.add_custom_button(__("Tambah Item"), () => show_item_step(frm)).addClass("btn-primary");
		},
	});

	function read_last_mode() {
		try {
			const m = localStorage.getItem(MODE_STORAGE_KEY);
			if (m === MODE_TOTAL || m === MODE_PER_WO) return m;
		} catch (e) {
			/* private browsing dsb. — abaikan */
		}
		return MODE_PER_WO;
	}

	function save_last_mode(mode) {
		try {
			localStorage.setItem(MODE_STORAGE_KEY, mode);
		} catch (e) {
			/* abaikan */
		}
	}

	// ---------- Langkah 1: Item & Qty ----------
	function show_item_step(frm, prefill) {
		let seq = 0; // race guard: hanya respons terbaru yang boleh menulis
		let suppress = false; // isian programatik tidak boleh memicu onchange
		// Validasi Link asinkron bisa memicu onchange SETELAH suppress ditutup;
		// nilai yang diisikan lewat fill() dicatat di sini dan dilewati sekali.
		let programmatic = {};

		const d = new frappe.ui.Dialog({
			title: __("Tambah Item — 1/2: Item & Qty"),
			fields: [
				{ fieldtype: "Section Break", label: __("Item") },
				{
					label: __("Kode Item"),
					fieldname: "item_code",
					fieldtype: "Link",
					options: "Item",
					reqd: 1,
					get_query: () => ({ filters: { item_group: "Produk Jadi" } }),
					onchange: item_change,
				},
				{ label: __("Nama Item"), fieldname: "item_name", fieldtype: "Data", read_only: 1 },
				{
					label: __("BOM"),
					fieldname: "bom_no",
					fieldtype: "Link",
					options: "BOM",
					reqd: 1,
					get_query: () => ({
						filters: { item: d.get_value("item_code") || "", is_active: 1 },
					}),
					onchange: bom_change,
				},
				{ label: __("Nama BOM"), fieldname: "bom_name", fieldtype: "Data", read_only: 1 },
				{ fieldtype: "Column Break" },
				{
					label: __("UOM Input"),
					fieldname: "uom",
					fieldtype: "Link",
					options: "UOM",
					reqd: 1,
					onchange: uom_change,
				},
				{
					label: __("Qty (UOM Input)"),
					fieldname: "qty_in_uom",
					fieldtype: "Float",
					reqd: 1,
					// onchange menangani set_value programatik; binding $input
					// di bawah menangani ketikan langsung (lebih responsif).
					onchange: update_summary,
				},
				{
					label: __("Faktor Konversi"),
					fieldname: "conversion_factor",
					fieldtype: "Float",
					read_only: 1,
					default: 1,
				},
				{ fieldname: "stock_uom", fieldtype: "Data", hidden: 1 },
				{ fieldtype: "Section Break" },
				{ fieldtype: "HTML", fieldname: "live_summary" },
			],
			primary_action_label: __("Lanjut →"),
			primary_action: () => {
				const values = d.get_values();
				if (!values) return;
				if (flt(values.qty_in_uom) <= 0) {
					frappe.msgprint(__("Qty harus lebih dari 0."));
					return;
				}
				if (flt(values.conversion_factor) <= 0) {
					frappe.msgprint(__("Faktor konversi tidak valid — periksa UOM Input."));
					return;
				}
				const item = collect();
				d.hide();
				show_alloc_step(frm, item);
			},
		});

		d.show();
		d.fields_dict.qty_in_uom.$input.on("input change", update_summary);

		if (prefill) {
			fill(prefill);
		} else {
			update_summary();
		}

		function collect() {
			return {
				item_code: d.get_value("item_code"),
				item_name: d.get_value("item_name") || "",
				bom_no: d.get_value("bom_no"),
				bom_name: d.get_value("bom_name") || "",
				stock_uom: d.get_value("stock_uom") || "",
				uom: d.get_value("uom"),
				qty_in_uom: flt(d.get_value("qty_in_uom")),
				conversion_factor: flt(d.get_value("conversion_factor")) || 1,
			};
		}

		function fill(values) {
			suppress = true;
			Object.keys(values).forEach((f) => {
				programmatic[f] = values[f];
				d.set_value(f, values[f]);
			});
			suppress = false;
			update_summary();
		}

		// true bila nilai ini berasal dari fill() dan belum pernah dilewati
		function consumed(field, value) {
			if (programmatic[field] !== undefined && programmatic[field] === value) {
				delete programmatic[field];
				return true;
			}
			return false;
		}

		function reset_item_fields() {
			fill({
				item_name: "",
				stock_uom: "",
				uom: "",
				bom_no: "",
				bom_name: "",
				qty_in_uom: "",
				conversion_factor: 1,
			});
		}

		function item_change() {
			if (suppress || consumed("item_code", d.get_value("item_code"))) return;
			const my = ++seq;
			const code = d.get_value("item_code");
			reset_item_fields();
			if (!code) return;

			frappe.call({
				method: `${API}.item_plan_info`,
				args: { item_code: code },
			}).then((r) => {
				if (my !== seq || !r.message) return; // user sudah pindah ke item lain
				const info = r.message;
				fill({
					item_name: info.item_name || "",
					stock_uom: info.stock_uom || "",
					uom: info.default_uom || info.stock_uom || "",
					conversion_factor: info.conversion_factor,
				});
				if (!info.found) {
					frappe.show_alert({
						message: __("Konversi UOM default tidak ditemukan untuk item ini — faktor diset 1."),
						indicator: "orange",
					});
				}
				if (info.bom) {
					fill({
						bom_no: info.bom.name,
						bom_name: info.bom.bom_name || "",
						qty_in_uom: qty_from_bom(info.bom.quantity, info.conversion_factor),
					});
				} else {
					frappe.show_alert({
						message: __("BOM default aktif tidak ditemukan — pilih BOM manual."),
						indicator: "orange",
					});
				}
				update_summary();
			});
		}

		function uom_change() {
			if (suppress || consumed("uom", d.get_value("uom"))) return;
			const code = d.get_value("item_code");
			const uom = d.get_value("uom");
			if (!code || !uom) return;
			const my = ++seq;

			frappe.call({
				method: `${API}.get_uom_conversion_factor`,
				args: { item_code: code, uom },
			}).then((r) => {
				if (my !== seq || !r.message) return;
				fill({ conversion_factor: r.message.conversion_factor });
				if (!r.message.found) {
					frappe.show_alert({
						message: __("Konversi {0} tidak ditemukan untuk item ini — faktor diset 1.", [uom]),
						indicator: "orange",
					});
				}
			});
			// Qty SENGAJA tidak dihitung ulang — jangan timpa input manual user.
		}

		function bom_change() {
			if (suppress || consumed("bom_no", d.get_value("bom_no"))) return;
			const bom_no = d.get_value("bom_no");
			if (!bom_no) {
				fill({ bom_name: "" });
				return;
			}
			const my = ++seq;

			frappe.call({
				method: `${API}.bom_info`,
				args: { bom_no },
			}).then((r) => {
				if (my !== seq || !r.message) return;
				const factor = flt(d.get_value("conversion_factor")) || 1;
				fill({
					bom_name: r.message.bom_name || "",
					qty_in_uom: qty_from_bom(r.message.quantity, factor),
				});
			});
		}

		function qty_from_bom(bom_quantity, factor) {
			if (bom_quantity === undefined || bom_quantity === null) return "";
			return flt(bom_quantity) / (flt(factor) || 1);
		}

		function update_summary() {
			const el = d.fields_dict.live_summary && d.fields_dict.live_summary.$wrapper;
			if (!el) return;
			const uom = d.get_value("uom");
			const qty = flt(d.get_value("qty_in_uom"));
			const factor = flt(d.get_value("conversion_factor"));
			const stock = d.get_value("stock_uom");
			if (!uom || !qty || !factor) {
				el.html('<span class="text-muted">' + __("Ringkasan muncul setelah item, UOM, dan qty terisi.") + "</span>");
				return;
			}
			el.html(
				__("Ringkasan") +
					": " +
					__("{0} {1} × {2} = <b>{3} {4}</b>", [
						format_number(qty),
						uom,
						format_number(factor),
						format_number(qty * factor),
						stock || __("stock UOM"),
					])
			);
		}
	}

	// ---------- Langkah 2: Mode + Jumlah WO + Preview + Apply ----------
	function show_alloc_step(frm, item) {
		const mode = read_last_mode();

		const d = new frappe.ui.Dialog({
			title: __("Tambah Item — 2/2: Alokasi Work Order"),
			fields: [
				{ fieldtype: "Section Break", label: __("Item Terpilih") },
				{ fieldtype: "HTML", fieldname: "item_overview" },
				{ fieldtype: "Section Break", label: __("Mode & Jumlah") },
				{ fieldname: "mode", fieldtype: "Data", hidden: 1, default: mode },
				{ fieldtype: "HTML", fieldname: "mode_selector" },
				{ fieldtype: "HTML", fieldname: "mode_desc" },
				{
					label: __("Jumlah Work Order"),
					fieldname: "num_wo",
					fieldtype: "Int",
					reqd: 1,
					default: 1,
					// onchange menangani set_value programatik; binding $input
					// menangani ketikan langsung; initial render jalan di bawah
					// setelah field siap (default belum terbaca saat construct).
					onchange: update_preview,
				},
				{ fieldtype: "Section Break", label: __("Preview Alokasi") },
				{ fieldtype: "HTML", fieldname: "preview" },
			],
			primary_action_label: __("✓ Tambah ke Assembly Items"),
			secondary_action_label: __("← Kembali"),
			primary_action: () => apply_rows(false),
			secondary_action: () => {
				d.hide();
				show_item_step(frm, item);
			},
		});

		d.show();
		d.$wrapper.find(".modal-dialog").addClass("pp-wizard-dialog");

		// Tombol ketiga (di luar primary/secondary bawaan Dialog): apply lalu
		// lanjut input item berikutnya tanpa menutup alur.
		d.$wrapper
			.find(".modal-footer")
			.prepend(
				$(`<button class="btn btn-default">${__("＋ Tambah & Input Lagi")}</button>`).on(
					"click",
					() => apply_rows(true)
				)
			);

		render_overview();
		bind_mode_selector();
		d.fields_dict.num_wo.$input.on("input change", update_preview);
		update_mode_desc(d.get_value("mode"));
		setTimeout(update_preview, 100); // field default baru terbaca setelah render

		function render_overview() {
			d.fields_dict.item_overview.$wrapper.html(`
				<table class="table table-bordered small" style="margin-bottom: 0;">
					<tbody>
						<tr>
							<td class="text-muted" style="width: 40%;">${__("Item")}</td>
							<td><b>${item.item_name || item.item_code}</b> <span class="text-muted">(${item.item_code})</span></td>
						</tr>
						<tr>
							<td class="text-muted">${__("BOM")}</td>
							<td>${item.bom_name || item.bom_no}</td>
						</tr>
						<tr>
							<td class="text-muted">${__("Qty Input")}</td>
							<td>${format_number(item.qty_in_uom)} ${item.uom}
								<span class="text-muted">(${format_number(item.qty_in_uom * item.conversion_factor)} ${item.stock_uom || "?"})</span></td>
						</tr>
					</tbody>
				</table>
			`);
		}

		function bind_mode_selector() {
			ensure_segmented_style();
			const is_total = d.get_value("mode") === MODE_TOTAL;
			d.fields_dict.mode_selector.$wrapper.html(`
				<div class="frappe-control">
					<div class="form-group">
						<div class="clearfix">
							<label class="control-label">${__("Mode Pembagian")}</label>
							<span class="reqd">*</span>
						</div>
						<div class="pp-wizard-seg">
							<button type="button" class="seg-btn ${is_total ? "active" : ""}" data-mode="${MODE_TOTAL}">
								${__("Total dibagi rata")}
							</button>
							<button type="button" class="seg-btn ${is_total ? "" : "active"}" data-mode="${MODE_PER_WO}">
								${__("Qty per Work Order")}
							</button>
						</div>
					</div>
				</div>
			`);
			d.fields_dict.mode_selector.$wrapper.find(".seg-btn").on("click", function () {
				d.fields_dict.mode_selector.$wrapper.find(".seg-btn").removeClass("active");
				$(this).addClass("active");
				const selected = $(this).attr("data-mode");
				d.set_value("mode", selected);
				update_mode_desc(selected);
				update_preview();
			});
		}

		function update_mode_desc(mode) {
			const desc =
				mode === MODE_TOTAL
					? __("ℹ️ <b>Total dibagi rata:</b> qty dibagi rata ke semua WO; sisa pembagian masuk baris terakhir.")
					: __("ℹ️ <b>Qty per Work Order:</b> setiap WO dibuat dengan qty input penuh di atas.");
			d.fields_dict.mode_desc.$wrapper.html(`<div class="text-muted small">${desc}</div>`);
		}

		// Alokasi selalu dihitung di stock UOM. Mode per-WO: tiap WO dapat qty
		// input penuh. Mode total: dibagi rata, baris terakhir = total −
		// Σ(sebelumnya) supaya Σ baris == total persis.
		function allocation(num_wo, mode) {
			let rows;
			if (mode === MODE_TOTAL) {
				const total_stock = item.qty_in_uom * item.conversion_factor;
				const base = total_stock / num_wo;
				rows = [];
				let used = 0;
				for (let i = 0; i < num_wo; i++) {
					const stock = i === num_wo - 1 ? total_stock - used : base;
					used += stock;
					rows.push(stock);
				}
			} else {
				const per = item.qty_in_uom * item.conversion_factor;
				rows = Array.from({ length: num_wo }, () => per);
			}
			return { total_stock: rows.reduce((a, b) => a + b, 0), rows };
		}

		function update_preview() {
			const num_wo = cint(d.get_value("num_wo")) || 0;
			const el = d.fields_dict.preview.$wrapper;
			if (num_wo <= 0) {
				el.html('<p class="text-muted small m-0">' + __("Isi jumlah Work Order untuk melihat preview.") + "</p>");
				return;
			}

			const alloc = allocation(num_wo, d.get_value("mode"));
			const is_fractional = alloc.rows.some(
				(r) => Math.abs(r - Math.round(r)) > 1e-9
			);

			let rows = "";
			const shown = Math.min(num_wo, PREVIEW_ROWS);
			for (let i = 0; i < shown; i++) {
				rows += `
					<tr>
						<td>WO ${i + 1}</td>
						<td class="text-right">${format_number(alloc.rows[i])} ${item.stock_uom || ""}</td>
						<td class="text-right text-muted">≈ ${format_number(alloc.rows[i] / item.conversion_factor)} ${item.uom}</td>
					</tr>`;
			}
			if (num_wo > PREVIEW_ROWS) {
				rows += `
					<tr>
						<td colspan="3" class="text-center text-muted italic small">
							${__("… dan {0} baris lainnya", [num_wo - PREVIEW_ROWS])}
						</td>
					</tr>`;
			}

			const notes = [];
			if (num_wo > WO_SOFT_LIMIT) {
				notes.push(
					`<div class="text-warning small">⚠ ${__("Jumlah Work Order besar ({0}) — pastikan bukan salah ketik.", [num_wo])}</div>`
				);
			}
			if (is_fractional) {
				notes.push(
					`<div class="text-warning small">⚠ ${__("Qty per Work Order tidak bulat dalam {0} — pastikan memang dimaksudkan.", [item.stock_uom || __("stock UOM")])}</div>`
				);
			}

			el.html(`
				<div class="table-responsive">
					<table class="table table-condensed table-bordered small m-0">
						<thead>
							<tr>
								<th>${__("Work Order")}</th>
								<th class="text-right">${__("Qty")} (${item.stock_uom || __("stock")})</th>
								<th class="text-right">${__("Qty")} (${item.uom || "?"})</th>
							</tr>
						</thead>
						<tbody>${rows}</tbody>
						<tfoot>
							<tr class="font-weight-bold table-active">
								<td>${__("Total")}</td>
								<td class="text-right">${format_number(alloc.total_stock)} ${item.stock_uom || ""}</td>
								<td class="text-right text-muted">≈ ${format_number(alloc.total_stock / item.conversion_factor)} ${item.uom}</td>
							</tr>
						</tfoot>
					</table>
				</div>
				${notes.join("")}
			`);
		}

		function apply_rows(again) {
			const values = d.get_values();
			if (!values) return;
			const num_wo = cint(values.num_wo);
			if (num_wo <= 0) {
				frappe.msgprint(__("Jumlah Work Order harus lebih dari 0."));
				return;
			}

			const mode = d.get_value("mode");
			const alloc = allocation(num_wo, mode);
			const first = alloc.rows[0];
			const last = alloc.rows[alloc.rows.length - 1];
			const uneven = num_wo > 1 && Math.abs(last - first) > 1e-9;
			const dup_count = (frm.doc.po_items || []).filter(
				(r) => r.item_code === item.item_code && r.bom_no === item.bom_no
			).length;

			let msg = __(
				"Tambahkan <b>{0} baris</b> untuk <b>{1}</b> ke Assembly Items?<br><br>Qty per Work Order: <b>≈ {2} {3}</b> ({4} {5})",
				[
					num_wo,
					item.item_name || item.item_code,
					format_number(first / item.conversion_factor),
					item.uom,
					format_number(first),
					item.stock_uom || __("stock UOM"),
				]
			);
			if (uneven) {
				msg += `<br>${__("Baris terakhir menampung sisa pembagian ({0} {1}).", [
					format_number(last),
					item.stock_uom || __("stock UOM"),
				])}`;
			}
			msg += `<br>${__("Total: <b>{0} {1}</b>", [format_number(alloc.total_stock), item.stock_uom || __("stock UOM")])}`;
			if (dup_count > 0) {
				msg += `<br><br>⚠ ${__(
					"Kombinasi item + BOM ini sudah ada {0} baris di tabel — baris baru tetap ditambahkan.",
					[dup_count]
				)}`;
			}

			frappe.confirm(msg, () => {
				// buang baris kosong bawaan form (tanpa item_code) supaya
				// Save tidak terblokir Missing Fields pada baris yang bukan milik wizard
				const blank = (frm.doc.po_items || []).filter((r) => !r.item_code).length;
				if (blank) {
					frm.doc.po_items = frm.doc.po_items.filter((r) => r.item_code);
				}
				alloc.rows.forEach((stock) => {
					const row = frm.add_child("po_items");
					row.item_code = item.item_code;
					row.bom_no = item.bom_no;
					row.planned_qty = stock;
					row.stock_uom = item.stock_uom;
				});
				frm.refresh_field("po_items");
				frm.dirty();

				save_last_mode(mode);
				frappe.show_alert({
					message: __("✓ {0} baris ditambahkan — jangan lupa Simpan.", [num_wo]),
					indicator: "green",
				});
				d.hide();
				if (again) {
					show_item_step(frm); // item berikutnya; mode teringat di localStorage
				}
			});
		}
	}

	// Segmented control ala Client Script lama; style disuntik sekali saja
	// (bukan per dialog) supaya tidak menumpuk <style> di DOM.
	function ensure_segmented_style() {
		if (document.getElementById("pp-wizard-style")) return;
		$(
			`<style id="pp-wizard-style">
				.pp-wizard-seg { display: flex; width: 100%; background-color: var(--control-bg, #f3f4f6); border: 1px solid var(--border-color, #e5e7eb); border-radius: var(--border-radius, 6px); padding: 4px; gap: 4px; }
				.pp-wizard-seg .seg-btn { flex: 1; padding: 6px 12px; font-size: var(--text-md, 13px); font-weight: 500; text-align: center; border: none; background: transparent; border-radius: var(--border-radius-sm, 4px); color: var(--text-muted, #6b7280); cursor: pointer; transition: all 0.2s ease; outline: none; }
				.pp-wizard-seg .seg-btn:hover { color: var(--text-color, #1f2937); background-color: var(--control-bg-on-hover, rgba(0,0,0,0.03)); }
				.pp-wizard-seg .seg-btn.active { background-color: var(--bg-color, #ffffff); box-shadow: var(--shadow-sm, 0 1px 2px rgba(0,0,0,0.08)); color: var(--text-color, #1f2937); cursor: default; }
				.pp-wizard-seg .seg-btn:focus-visible { box-shadow: 0 0 0 2px var(--primary-color, #2490ef); }
				.pp-wizard-dialog .modal-body { max-height: calc(100vh - 190px); overflow-y: auto; }
			</style>`
		).appendTo("head");
	}
})();
