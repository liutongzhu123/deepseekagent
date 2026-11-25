(function attach(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.CrmPriceInput = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function createApi() {
  function preserveNumericText(quantity, unitPrice, discount) {
    const values = {
      quantity: String(quantity ?? '').trim(),
      unit_price: String(unitPrice ?? '').trim(),
      discount: String(discount ?? '').trim(),
    };
    const parsed = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, Number(value)]));
    if (!values.quantity || !values.unit_price || !values.discount
        || !Object.values(parsed).every(Number.isFinite)
        || parsed.quantity <= 0 || parsed.unit_price < 0
        || parsed.discount < 0 || parsed.discount > 100) {
      throw new Error('请检查数量、单价和折扣，必须为有效范围内的数字');
    }
    // Keep the raw decimal strings for the server's exact underflow/precision
    // checks. Number() above is only for range checks, never for serialization.
    return values;
  }
  return { preserveNumericText };
});
