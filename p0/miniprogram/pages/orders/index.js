const store = require('../../utils/mock-store');

Page({
  data: { quotes: [] },
  onShow() { this.setData({ quotes: store.listQuotes() }); },
  goQuote() { wx.navigateTo({ url: '/pages/quote/index' }); },
  clear() {
    wx.showModal({
      title: '清空本机演示数据？',
      content: '此操作会删除本小程序保存的模拟需求和客服消息。',
      success: (result) => {
        if (result.confirm && store.clearDemoData()) this.setData({ quotes: [] });
      },
    });
  },
});
