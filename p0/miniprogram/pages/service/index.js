const store = require('../../utils/mock-store');

Page({
  data: { messages: [], draft: '' },
  onShow() { this.refresh(); },
  refresh() { this.setData({ messages: store.listMessages() }); },
  onInput(event) { this.setData({ draft: event.detail.value }); },
  chooseQuestion(event) {
    const question = event.currentTarget.dataset.question;
    this.setData({ draft: question }, () => this.send());
  },
  send() {
    const text = this.data.draft.trim();
    if (!text) return;
    const messages = store.addMessage(text);
    if (!messages) {
      wx.showToast({ title: '本机存储失败，请重试', icon: 'none' });
      return;
    }
    this.setData({ messages, draft: '' });
    wx.pageScrollTo({ scrollTop: 999999, duration: 200 });
  },
});
