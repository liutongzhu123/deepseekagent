const store = require('../../utils/mock-store');
const serviceApi = require('../../utils/service-api');

const GUIDED_ANSWERS = {
  '我想咨询检测项目和能力范围': '请先说明拟检测产品、项目或标准及样品数量。实际能力范围需由业务人员按资质、设备及样品要求核实；此页面为离线演示，不代表已受理或确认可检测。',
  '我想咨询检测周期': '请提供检测项目、标准、样品数量和期望完成日期。实际周期需结合当前排期与样品状态由业务人员确认；演示页面不提供时效承诺。',
  '我想咨询委托流程': '演示路径：在“我的”填写虚构资料，再到“业务记录”选择模拟模板创建委托。记录仅保存在当前设备，不会提交、受理或触发审核。正式流程请以业务人员提供的要求为准。',
};

Page({
  data: { messages: [], draft: '', apiEnabled: false, conversationId: '', serviceError: '', sending: false, pendingBlocked: false, pendingUnreadable: false, pendingClearedNeedsCheck: false },
  onShow() { this.refresh(); },
  async refresh() {
    const apiEnabled = serviceApi.configured();
    const pending = apiEnabled ? serviceApi.readPending() : null;
    this.setData({
      apiEnabled,
      draft: pending && !pending.blocked ? pending.text : this.data.draft,
      serviceError: pending && pending.blocked
        ? (pending.needsCheck ? '本机客服消息重试编号已清理；请刷新并核对服务端会话，再确认继续。' : (pending.unreadable ? '本机客服消息待办不可读取，已停止提交以避免重复消息。请清理本机待办后重试。' : '这台设备上有另一个登记账号未确认的演示消息。为保护账号资料，请先清除旧消息。'))
        : '',
      pendingBlocked: Boolean(pending && pending.blocked),
      pendingUnreadable: Boolean(pending && pending.unreadable),
      pendingClearedNeedsCheck: Boolean(pending && pending.needsCheck),
    });
    if (!apiEnabled) { this.setData({ messages: store.listMessages(), conversationId: '' }); return; }
    try {
      const rows = await serviceApi.list();
      const preferred = rows.find((row) => row.conversation_id === serviceApi.readLast() && row.status !== 'archived');
      const conversation = preferred || rows.find((row) => row.status !== 'archived');
      serviceApi.writeLast(conversation ? conversation.conversation_id : '');
      this.setData({
        conversationId: conversation ? conversation.conversation_id : '',
        draft: pending && !pending.blocked ? pending.text : this.data.draft,
        pendingBlocked: Boolean(pending && pending.blocked),
        serviceError: pending && pending.blocked
          ? (pending.needsCheck ? '本机客服消息重试编号已清理；请刷新并核对服务端会话，再确认继续。' : (pending.unreadable ? '本机客服消息待办不可读取，已停止提交以避免重复消息。请清理本机待办后重试。' : '这台设备上有另一个登记账号未确认的演示消息。为保护账号资料，请先清除旧消息。'))
          : '',
        messages: conversation ? conversation.messages.map((item, index) => ({
          id: `${item.at}-${index}`, side: item.sender === 'customer' ? 'user' : 'agent', text: item.text,
        })) : [],
      });
    } catch (error) {
      this.setData({ conversationId: '', messages: [], serviceError: error.message || '客服演示服务不可用' });
    }
  },
  onInput(event) { this.setData({ draft: event.detail.value }); },
  discardPending() {
    if (this.data.pendingClearedNeedsCheck) {
      if (!serviceApi.acknowledgeCleanupCheck()) {
        this.setData({ serviceError: '无法保存核对确认，请重试' });
        return;
      }
      this.setData({ pendingClearedNeedsCheck: false });
      this.refresh();
      return;
    }
    wx.showModal({
      title: '清理本机客服待办？',
      content: '这会删除本机保存的客服消息重试编号。若服务端已经收到消息，服务器会话不会回退；清理后请先刷新会话并核对记录，再发送新消息。',
      success: (result) => {
        if (!result.confirm) return;
        if (!serviceApi.discardPending()) {
          this.setData({ serviceError: '无法清除设备上的待确认消息' });
          return;
        }
        this.setData({ pendingBlocked: false, pendingUnreadable: false, pendingClearedNeedsCheck: true, serviceError: '本机消息重试编号已清理。请先刷新并核对会话记录；确认后再发送新消息。' });
      },
    });
  },
  chooseQuestion(event) {
    const question = event.currentTarget.dataset.question;
    const answer = GUIDED_ANSWERS[question];
    if (!answer) return;
    if (serviceApi.configured()) { this.sendServiceMessage(question); return; }
    const messages = store.addGuidedExchange(question, answer);
    if (!messages) {
      wx.showToast({ title: '本机存储失败，请重试', icon: 'none' });
      return;
    }
    this.setData({ messages, draft: '' });
    wx.pageScrollTo({ scrollTop: 999999, duration: 200 });
  },
  send() {
    if (this.data.pendingClearedNeedsCheck) {
      wx.showToast({ title: '请先刷新并核对会话记录，再确认继续', icon: 'none' });
      return;
    }
    const text = this.data.draft.trim();
    if (!text) return;
    if (serviceApi.configured()) { this.sendServiceMessage(text); return; }
    const messages = store.addMessage(text);
    if (!messages) {
      wx.showToast({ title: '本机存储失败，请重试', icon: 'none' });
      return;
    }
    this.setData({ messages, draft: '' });
    wx.pageScrollTo({ scrollTop: 999999, duration: 200 });
  },
  async sendServiceMessage(text) {
    if (this.data.pendingClearedNeedsCheck) {
      wx.showToast({ title: '请先刷新并核对会话记录，再确认继续', icon: 'none' });
      return;
    }
    if (this.data.sending) return;
    this.setData({ sending: true, serviceError: '' });
    try {
      const conversation = await serviceApi.send(text, this.data.conversationId);
      serviceApi.writeLast(conversation.conversation_id);
      this.setData({
        conversationId: conversation.conversation_id,
        draft: '',
        messages: conversation.messages.map((item, index) => ({
          id: `${item.at}-${index}`, side: item.sender === 'customer' ? 'user' : 'agent', text: item.text,
        })),
      });
      wx.pageScrollTo({ scrollTop: 999999, duration: 200 });
    } catch (error) {
      this.setData({ serviceError: error.message || '消息未存档' });
      wx.showToast({ title: error.message || '消息未存档', icon: 'none' });
    } finally { this.setData({ sending: false }); }
  },
});
