const quoteHandoff = require('../../utils/quote-handoff');
const serviceApi = require('../../utils/service-api');
const store = require('../../utils/mock-store');

Page({
  data: { project: '', standard: '', quantity: '', urgency: '常规', details: '', missingFields: [], guidance: '', apiEnabled: false, submitting: false, handoffBusy: false, submitError: '' },
  onLoad() { this.setData({ apiEnabled: serviceApi.configured() }); this.refreshGuidance(); },
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
  async handoff() {
    if (this.data.handoffBusy) return;
    const apiEnabled = serviceApi.configured();
    this.setData({ handoffBusy: true, submitError: '' });
    try {
      await quoteHandoff.handoff(this.data);
      wx.switchTab({ url: '/pages/service/index' });
    } catch (error) {
      this.setData({ submitError: error.message || '客服演示提交失败' });
      wx.showToast({ title: error.message || '提交失败', icon: 'none', duration: 3000 });
    } finally { this.setData({ handoffBusy: false }); }
  },
  async submit() {
    if (this.data.submitting) return;
    if (!this.data.project.trim()) {
      wx.showToast({ title: '请填写检测项目', icon: 'none' });
      return;
    }
    if (serviceApi.configured()) {
      this.setData({ submitting: true, submitError: '' });
      try {
        await quoteHandoff.submit(this.data);
        wx.showModal({
          title: '已提交本地演示队列',
          content: '需求已进入客服工作台，尚未生成价格或正式报价。可到客服页查看对话并刷新人工回复。',
          showCancel: false,
          success: () => wx.switchTab({ url: '/pages/service/index' }),
        });
      } catch (error) {
        this.setData({ submitError: error.message || '提交失败；没有保存为已提交' });
        wx.showToast({ title: error.message || '提交失败', icon: 'none', duration: 3000 });
      } finally { this.setData({ submitting: false }); }
      return;
    }
    let quote;
    try { quote = (await quoteHandoff.submit(this.data)).quote; }
    catch (error) { wx.showToast({ title: error.message || '本机存储失败', icon: 'none' }); return; }
    wx.showModal({
      title: quote.status,
      content: `${quote.guidance}本版本不生成价格、不提交后台。`,
      showCancel: false,
      success: () => wx.switchTab({ url: '/pages/orders/index' }),
    });
  },
});
