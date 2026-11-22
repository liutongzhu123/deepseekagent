Page({
  data: { count: 0 },
  onShow() {
    const store = require('../../utils/mock-store');
    this.setData({ count: store.listQuotes().length });
  },
  goQuote() { wx.navigateTo({ url: '/pages/quote/index' }); },
  goService() { wx.switchTab({ url: '/pages/service/index' }); },
  goOrders() { wx.switchTab({ url: '/pages/orders/index' }); },
});
