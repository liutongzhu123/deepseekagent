(function (root) {
  function createSecureOperationId(prefix, cryptoApi = root.crypto) {
    if (!cryptoApi || typeof cryptoApi.getRandomValues !== 'function') {
      throw new Error('当前浏览器无法生成安全重试编号，操作未发送');
    }
    const bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    return `${prefix}-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = createSecureOperationId;
  else root.createSecureOperationId = createSecureOperationId;
})(typeof window === 'undefined' ? globalThis : window);
