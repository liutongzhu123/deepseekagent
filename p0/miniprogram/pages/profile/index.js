const store = require('../../utils/mock-store');

Page({
  data: { quoteCount: 0, messageCount: 0, orderCount: 0, profile: null, companyName: '', contactName: '', region: '' },
  onShow() {
    const profile = store.getProfile();
    this.setData({
      quoteCount: store.listQuotes().length,
      messageCount: Math.max(0, store.listMessages().length - 1),
      orderCount: store.listOrders().length,
      profile,
      companyName: profile ? profile.companyName : '',
      contactName: profile ? profile.contactName : '',
      region: profile ? profile.region : '',
    });
  },
  onInput(event) { this.setData({ [event.currentTarget.dataset.field]: event.detail.value }); },
  saveProfile() {
    const profile = store.saveProfile(this.data);
    if (!profile) {
      wx.showToast({ title: '请填写企业、联系人和地区', icon: 'none' });
      return;
    }
    this.setData({ profile });
    wx.showToast({ title: '已保存在本机', icon: 'success' });
  },
  clear() {
    wx.showModal({
      title: '重置演示数据？',
      content: '将清除本机客户资料、模拟需求、委托单和客服消息记录。',
      success: (result) => {
        if (!result.confirm) return;
        const cleared = store.clearDemoData();
        this.onShow();
        wx.showToast({ title: cleared ? '已清除' : '部分数据未清除', icon: cleared ? 'success' : 'none' });
      },
    });
  },
});
