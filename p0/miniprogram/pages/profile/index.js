const store = require('../../utils/mock-store');

Page({
  data: { quoteCount: 0, messageCount: 0 },
  onShow() {
    this.setData({ quoteCount: store.listQuotes().length, messageCount: store.listMessages().length - 1 });
  },
  clear() {
    wx.showModal({
      title: '重置演示数据？',
      content: '将清除本机的模拟需求和客服消息记录。',
      success: (result) => {
        if (result.confirm && store.clearDemoData()) {
          this.setData({ quoteCount: 0, messageCount: 0 });
          wx.showToast({ title: '已清除', icon: 'success' });
        }
      },
    });
  },
});
