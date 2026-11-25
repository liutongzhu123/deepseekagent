function read(key) {
  try {
    const info = wx.getStorageInfoSync();
    if (!info || !Array.isArray(info.keys)) return { ok: false, present: false, value: null };
    if (!info.keys.includes(key)) return { ok: true, present: false, value: null };
    const value = wx.getStorageSync(key);
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      return { ok: false, present: true, value: null };
    }
    return { ok: true, present: true, value };
  }
  catch (error) { return { ok: false, value: null }; }
}

function requireRead(key, label) {
  const result = read(key);
  if (!result.ok) throw new Error(`${label}本机记录不可读取或格式无效，已停止提交以避免重复操作`);
  return result.value;
}

function isOperationId(value) {
  return typeof value === 'string' && /^[\x21-\x7e]{16,128}$/.test(value);
}

module.exports = { read, requireRead, isOperationId };
