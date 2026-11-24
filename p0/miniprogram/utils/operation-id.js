// Reusable mutation IDs must not rely on Math.random or fall back to a weaker source.
function createOperationId(prefix) {
  return new Promise((resolve, reject) => {
    if (!wx.getRandomValues) {
      reject(new Error('当前微信基础库不支持安全随机数，操作未发送'));
      return;
    }
    wx.getRandomValues({
      length: 32,
      success(result) {
        const bytes = new Uint8Array(result.randomValues);
        if (bytes.length !== 32) {
          reject(new Error('安全随机数长度异常，操作未发送'));
          return;
        }
        let hex = '';
        bytes.forEach((value) => { hex += value.toString(16).padStart(2, '0'); });
        resolve(`${prefix}-${hex}`);
      },
      fail() { reject(new Error('无法生成安全操作编号，操作未发送')); },
    });
  });
}

module.exports = createOperationId;
