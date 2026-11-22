const store = require('../../utils/mock-store');

Page({
  data: { project: '', standard: '', quantity: '', urgency: '常规', details: '', missingFields: [], guidance: '' },
  onLoad() { this.refreshGuidance(); },
  onInput(event) {
    this.setData({ [event.currentTarget.dataset.field]: event.detail.value }, () => this.refreshGuidance());
  },
  chooseUrgency(event) { this.setData({ urgency: event.currentTarget.dataset.value }); },
  refreshGuidance() {
    const missingFields = store.missingQuoteFields(this.data);
    this.setData({
      missingFields,
      guidance: missingFields.length
        ? `建议补充：${missingFields.join('、')}。可先保存为待补充需求。`
        : '检测项目、标准和数量已填写，可提交给业务人员确认（演示）。',
    });
  },
  handoff() {
    const context = store.quoteContext(this.data);
    if (!context) {
      wx.showToast({ title: '请先填写检测项目', icon: 'none' });
      return;
    }
    if (!store.addMessage(context)) {
      wx.showToast({ title: '本机存储失败，请重试', icon: 'none' });
      return;
    }
    wx.switchTab({ url: '/pages/service/index' });
  },
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
      title: quote.status,
      content: `${quote.guidance}本版本不生成价格、不提交后台。`,
      showCancel: false,
      success: () => wx.switchTab({ url: '/pages/orders/index' }),
    });
  },
});
