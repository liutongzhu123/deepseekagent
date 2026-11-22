const store = require('../../utils/mock-store');

Page({
  data: { quotes: [], orders: [] },
  onShow() { this.refresh(); },
  refresh() { this.setData({ quotes: store.listQuotes(), orders: store.listOrders() }); },
  goQuote() { wx.navigateTo({ url: '/pages/quote/index' }); },
  createOrder() {
    if (!store.getProfile()) {
      wx.showModal({
        title: '先登记演示客户资料',
        content: '委托单将从本机演示资料带入企业名称和联系人。',
        success: (result) => {
          if (result.confirm) wx.switchTab({ url: '/pages/profile/index' });
        },
      });
      return;
    }
    wx.navigateTo({ url: '/pages/orders/create' });
  },
  clear() {
    wx.showModal({
      title: '清空本机演示数据？',
      content: '此操作会删除本小程序保存的客户资料、模拟需求、委托单和客服消息。',
      success: (result) => {
        if (!result.confirm) return;
        const cleared = store.clearDemoData();
        this.refresh();
        wx.showToast({ title: cleared ? '已清除' : '部分数据未清除', icon: cleared ? 'success' : 'none' });
      },
    });
  },
});
