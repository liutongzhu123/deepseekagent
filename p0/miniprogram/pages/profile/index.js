const store = require('../../utils/mock-store');
const registrationApi = require('../../utils/registration-api');
const serviceApi = require('../../utils/service-api');
const orderApi = require('../../utils/order-api');

Page({
  data: {
    quoteCount: 0, messageCount: 0, orderCount: 0, profile: null,
    companyName: '', contactName: '', region: '', registrationApiConfigured: false,
    registrationStatus: '', registrationBusy: false, registrationPending: false, registrationPendingBlocked: false, registrationPendingUnreadable: false, registrationPendingClearedNeedsCheck: false,
  },
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
      registrationApiConfigured: registrationApi.configured(),
    });
    const capability = registrationApi.readCapability();
    const pending = registrationApi.readPending();
    if (pending && pending.payload) {
      this.setData({
        companyName: pending.payload.company_name,
        contactName: pending.payload.contact_name,
        region: pending.payload.region,
        registrationStatus: capability ? '上次资料更新结果待确认' : '上次创建结果待确认',
        registrationPending: true,
        registrationPendingBlocked: false,
        registrationPendingUnreadable: false,
      });
    } else if (pending && pending.blocked) {
      this.setData({
        registrationStatus: pending.unreadable ? '待办记录不可读取' : '另一登记操作待恢复',
        registrationPending: true, registrationPendingBlocked: true,
        registrationPendingUnreadable: Boolean(pending.unreadable),
        registrationPendingClearedNeedsCheck: Boolean(pending.needsCheck),
      });
    } else {
      this.setData({ registrationPending: false, registrationPendingBlocked: false, registrationPendingUnreadable: false, registrationPendingClearedNeedsCheck: false, registrationStatus: capability ? this.data.registrationStatus : '' });
    }
    if (capability && registrationApi.configured()) {
      this.setData({ registrationStatus: (capability.registration || {}).status || '正在查询' });
      registrationApi.refresh().then((registration) => {
        this.setData({ registrationStatus: registration ? registration.status : '' });
      }).catch(() => {
        this.setData({ registrationStatus: '查询失败' });
      });
    }
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
  async submitRegistration() {
    if (this.data.registrationPendingClearedNeedsCheck) {
      wx.showToast({ title: '请先核对服务端登记状态并确认继续', icon: 'none' });
      return;
    }
    if (!registrationApi.configured()) {
      wx.showToast({ title: '演示 API 未配置，资料未提交', icon: 'none' });
      return;
    }
    if (this.data.registrationBusy) return;
    this.setData({ registrationBusy: true });
    try {
      const registration = await registrationApi.submit({
        company_name: this.data.companyName,
        contact_name: this.data.contactName,
        region: this.data.region,
      });
      this.setData({ registrationStatus: registration.status, registrationPending: Boolean(registrationApi.readPending()) });
      wx.showToast({ title: '本地演示登记已提交', icon: 'success' });
    } catch (error) {
      const pending = registrationApi.readPending();
      if (pending && pending.payload) {
        this.setData({
          companyName: pending.payload.company_name,
          contactName: pending.payload.contact_name,
          region: pending.payload.region,
          registrationStatus: registrationApi.readCapability() ? '上次资料更新结果待确认' : '上次创建结果待确认',
          registrationPending: true,
          registrationPendingBlocked: false,
          registrationPendingUnreadable: false,
        });
      } else if (pending && pending.blocked) {
        this.setData({
          registrationPending: true, registrationPendingBlocked: true,
          registrationPendingUnreadable: Boolean(pending.unreadable),
          registrationStatus: pending.unreadable ? '待办记录不可读取' : '另一登记操作待恢复',
        });
      } else this.setData({ registrationPending: false, registrationPendingBlocked: false, registrationPendingUnreadable: false, registrationStatus: '' });
      wx.showToast({ title: error.message || '演示服务不可用，资料未提交', icon: 'none', duration: 3000 });
    } finally {
      this.setData({ registrationBusy: false });
    }
  },
  async refreshRegistration() {
    try {
      const registration = await registrationApi.refresh();
      this.setData({ registrationStatus: registration ? registration.status : '尚未创建' });
    } catch (error) {
      this.setData({ registrationStatus: '查询失败' });
      wx.showToast({ title: '状态查询失败，请稍后重试', icon: 'none' });
    }
  },
  clearRegistrationPending() {
    if (this.data.registrationPendingClearedNeedsCheck) {
      if (!registrationApi.acknowledgeCleanupCheck()) {
        wx.showToast({ title: '无法保存核对确认，请重试', icon: 'none' });
        return;
      }
      this.setData({ registrationPendingClearedNeedsCheck: false, registrationStatus: '已确认核对服务端登记状态' });
      this.onShow();
      return;
    }
    wx.showModal({
      title: '清理登记待办？',
      content: '这会删除本机登记重试编号和保存的登记凭证。若服务端已创建或更新登记，服务器记录不会回退；清理后请先联系管理员核对，再重新登记。',
      success: (result) => {
        if (!result.confirm) return;
        const cleared = registrationApi.forgetPending() && registrationApi.forgetCapability();
        if (cleared) this.setData({ registrationPending: false, registrationPendingBlocked: false, registrationPendingUnreadable: false, registrationPendingClearedNeedsCheck: true, registrationStatus: '本机登记状态已清理，请先核对服务端状态' });
        wx.showToast({ title: cleared ? '已清理本机待办' : '清理失败，请稍后重试', icon: cleared ? 'success' : 'none' });
      },
    });
  },
  clear() {
    const hasPendingCreate = Boolean(registrationApi.readPending());
    wx.showModal({
      title: '重置演示数据？',
      content: hasPendingCreate
        ? '将清除本机资料和未确认登记操作的重试编号。若服务端已创建或更新登记，服务器上的演示记录不会回退或删除。'
        : '将清除本机客户资料、演示状态凭证、模拟需求、委托单和客服消息记录；服务端的演示登记不会删除。',
      success: (result) => {
        if (!result.confirm) return;
        const checksPrepared = registrationApi.prepareCleanupCheck()
          && orderApi.prepareCleanupCheck()
          && serviceApi.prepareDiscardCheck();
        if (!checksPrepared) {
          this.onShow();
          wx.showToast({ title: '重置未执行；请按页面提示处理已保存的核对标记', icon: 'none', duration: 3000 });
          return;
        }
        const cleared = store.clearDemoData();
        const tokenCleared = registrationApi.forgetCapability();
        const pendingCleared = registrationApi.forgetPending();
        const orderPendingCleared = orderApi.forgetPending();
        const conversationCleared = serviceApi.forgetLast() && serviceApi.discardPending();
        this.onShow();
        const success = cleared && tokenCleared && pendingCleared && orderPendingCleared && conversationCleared;
        wx.showToast({ title: success ? '已清除本机数据' : '部分数据未清除', icon: success ? 'success' : 'none' });
      },
    });
  },
});
