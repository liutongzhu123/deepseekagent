const store = require('../../utils/mock-store');

Page({
  data: { project: '', standard: '', quantity: '', urgency: '常规', details: '' },
  onInput(event) {
    this.setData({ [event.currentTarget.dataset.field]: event.detail.value });
  },
  chooseUrgency(event) { this.setData({ urgency: event.currentTarget.dataset.value }); },
  submit() {
    if (!this.data.project.trim()) {
      wx.showToast({ title: '请填写检测项目', icon: 'none' });
      return;
    }
    const quote = store.addQuote(this.data);
    if (!quote) {
      wx.showToast({ title: '本机存储失败，请重试', icon: 'none' });
      return;
    }
    wx.showModal({
      title: '模拟需求已保存',
      content: '本地演示状态为“待业务人员确认”。本版本不生成价格、不提交后台。',
      showCancel: false,
      success: () => wx.switchTab({ url: '/pages/orders/index' }),
    });
  },
});
