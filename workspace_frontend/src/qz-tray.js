import qz from 'qz-tray';

function configureSigning(workOrder) {
  qz.security.setCertificatePromise(async () => {
    const response = await fetch(`/api/method/production_app.api.qz_signing.certificate?work_order=${encodeURIComponent(workOrder)}`, {
      cache: 'no-store'
    });
    if (!response.ok) throw new Error('Sertifikat QZ Tray belum tersedia atau akses ditolak');
    return (await response.json()).message;
  }, { rejectOnFailure: true });

  qz.security.setSignatureAlgorithm('SHA512');
  qz.security.setSignaturePromise(async request => {
    const response = await fetch('/api/method/production_app.api.qz_signing.sign', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Frappe-CSRF-Token': window.csrf_token || ''
      },
      credentials: 'same-origin',
      cache: 'no-store',
      body: JSON.stringify({ work_order: workOrder, request })
    });
    if (!response.ok) throw new Error('Gagal menandatangani permintaan QZ Tray');
    return (await response.json()).message;
  });
}

export async function connectQz(workOrder) {
  configureSigning(workOrder);
  if (!qz.websocket.isActive()) {
    await qz.websocket.connect({ retries: 2, delay: 1 });
  }
}

export async function printLabels(workOrder, extraCount = 0) {
  try {
    await connectQz(workOrder);
    const res = await fetch('/api/method/production_app.api.work_order.get_label_data', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-Frappe-CSRF-Token': window.csrf_token || ''
      },
      body: JSON.stringify({ name: workOrder, extra: extraCount })
    });
    const data = await res.json();
    if (!res.ok) {
      let detail = '';
      try {
        const messages = JSON.parse(data._server_messages || '[]');
        detail = messages.map(message => JSON.parse(message).message).filter(Boolean).join(' ');
      } catch (_error) {
        // Keep the generic message if Frappe did not return structured errors.
      }
      throw new Error(detail || 'Gagal mengambil data label');
    }
    const { message: labelData } = data;

    if (!labelData) {
      throw new Error('Data label kosong');
    }

    let printer = await qz.printers.getDefault();

    const labels = [];
    const count = labelData.labels?.length || 0;
    if (!count || count !== labelData.label_count) throw new Error('Jumlah serial label tidak sesuai');

    for (let i = 0; i < count; i += 2) {
      let zpl = `^XA\n^PW800\n^LL160\n`;

      // First column (left)
      zpl += getSingleLabelZpl(labelData, labelData.labels[i], 0, i + 1);

      // Second column (right)
      if (i + 1 < count) {
        zpl += getSingleLabelZpl(labelData, labelData.labels[i + 1], 400, i + 2);
      }

      zpl += `^XZ\n`;
      labels.push(zpl);
    }

    const config = qz.configs.create(printer, {
      encoding: 'UTF-8'
    });

    await qz.print(config, labels);

    return true;
  } catch (err) {
    console.error('QZ Tray Error:', err);
    throw err;
  }
}

function zplValue(value) {
  return String(value ?? '').replace(/[_^~\\]/g, char => ({
    _: '_5F', '^': '_5E', '~': '_7E', '\\': '_5C'
  })[char]);
}

function getSingleLabelZpl(data, label, offsetX, seq) {
  const mfg = data.manufacturing_date ? formatDate(data.manufacturing_date) : '-';
  const exp = data.expiry_date ? formatDate(data.expiry_date) : '-';

  let zpl = '';

  // Helper untuk membuat teks bold
  const bText = (x, y, h, w, txt, fbOpts = '') => {
    return `^FO${x},${y}^A0N,${h},${w}${fbOpts}^FD${txt}^FS\n` +
      `^FO${x + 1},${y}^A0N,${h},${w}${fbOpts}^FD${txt}^FS\n`;
  };

  // Label Sequence Top Right (Digeser manual ke kanan)
  zpl += bText(offsetX + 355, 28, 18, 18, `${seq}`, `^FB40,1,0,R`);

  // --- QR BLOCK (Sisi Kiri - Digeser manual ke kanan) ---
  zpl += `^FO${offsetX + 68},32^BQN,2,4^FDQA,${zplValue(label.qr_value)}^FS\n`;

  // --- INFO BLOCK (Sisi Kanan - Digeser manual ke kanan) ---
  let y = 32;
  if (data.item_name_prefix) {
    zpl += bText(offsetX + 192, y, 18, 18, data.item_name_prefix);
    y += 20;
    zpl += bText(offsetX + 192, y, 26, 26, data.item_name_main || '');
    y += 28;
  } else {
    zpl += bText(offsetX + 192, y, 26, 26, data.item_name_main || data.item_name || '');
    y += 30;
  }

  // Teks Informasi Tambahan (SKU, MFG, EXP)
  zpl += bText(offsetX + 192, y, 21, 21, `SKU : ${data.sku}`);
  y += 24;
  zpl += bText(offsetX + 192, y, 21, 21, `MFG : ${mfg}`);
  y += 24;
  zpl += bText(offsetX + 192, y, 21, 21, `EXP : ${exp}`);

  return zpl;
}

function formatDate(dateStr) {
  if (!dateStr) return '-';
  const [y, m, d] = dateStr.split('-');
  const yy = y.slice(-2);
  return `${d}-${m}-${yy}`;
}
