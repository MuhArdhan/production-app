import qz from 'qz-tray';

let isConnected = false;

export async function connectQz() {
  if (isConnected) return;
  if (!qz.websocket.isActive()) {
    await qz.websocket.connect({ retries: 2, delay: 1 });
  }
  isConnected = true;
}

export async function printLabels(workOrder, extraCount = 0) {
  try {
    const res = await fetch(`/api/method/production_app.api.work_order.get_label_data?name=${encodeURIComponent(workOrder)}&extra=${extraCount}`);
    if (!res.ok) {
      throw new Error('Gagal mengambil data label');
    }
    const data = await res.json();
    const { message: labelData } = data;

    if (!labelData) {
      throw new Error('Data label kosong');
    }

    await connectQz();

    // Default printer or prompt user, QZ Tray allows finding printers
    // For now, we find a default ZPL printer or just the default printer
    let printer = await qz.printers.getDefault();

    // Generate ZPL
    // 2 columns per label. Label size 50x20 mm. 
    // Assuming 203 DPI -> 8 dots/mm -> 400 dots width, 160 dots height per label.
    // Total width = 800 dots.
    const labels = [];
    const count = labelData.label_count;

    for (let i = 0; i < count; i += 2) {
      // We print two items per row
      let zpl = `^XA\n^PW800\n^LL160\n`; // 800 width (2x400), 160 height

      // First column (left)
      zpl += getSingleLabelZpl(labelData, 0, i + 1, count);

      // Second column (right)
      if (i + 1 < count) {
        zpl += getSingleLabelZpl(labelData, 400, i + 2, count);
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

function getSingleLabelZpl(data, offsetX, seq, total) {
  const mfg = data.manufacturing_date ? formatDate(data.manufacturing_date) : '-';
  const exp = data.expiry_date ? formatDate(data.expiry_date) : '-';

  let zpl = '';

  // Helper to simulate bold text by printing twice with 1 dot X offset
  const bText = (x, y, h, w, txt, fbOpts = '') => {
    return `^FO${x},${y}^A0N,${h},${w}${fbOpts}^FD${txt}^FS\n` +
      `^FO${x + 1},${y}^A0N,${h},${w}${fbOpts}^FD${txt}^FS\n`;
  };

  // Label Sequence Top Right
  zpl += bText(offsetX + 340, 25, 16, 16, `${seq}/${total}`, `^FB50,1,0,R`);

  // QR Block (Left, geser agar di tengah)
  zpl += `^FO${offsetX + 95},55^BQN,2,3^FDQA,${data.sku}^FS\n`;
  zpl += bText(offsetX + 82, 133, 16, 16, data.sku, `^FB80,1,0,C`);

  // Info Block (Right, geser agar berimbang)
  let y = 50;
  if (data.item_name_prefix) {
    zpl += bText(offsetX + 195, y, 16, 16, data.item_name_prefix);
    y += 18;
    zpl += bText(offsetX + 195, y, 22, 22, data.item_name_main || '');
  } else {
    zpl += bText(offsetX + 195, y, 22, 22, data.item_name_main || data.item_name || '');
  }

  y += 30;
  zpl += bText(offsetX + 195, y, 18, 18, `SKU : ${data.sku}`);
  y += 20;
  zpl += bText(offsetX + 195, y, 18, 18, `MFG : ${mfg}`);
  y += 20;
  zpl += bText(offsetX + 195, y, 18, 18, `EXP : ${exp}`);

  return zpl;
}

function formatDate(dateStr) {
  if (!dateStr) return '-';
  const [y, m, d] = dateStr.split('-');
  const yy = y.slice(-2);
  return `${d}-${m}-${yy}`;
}
