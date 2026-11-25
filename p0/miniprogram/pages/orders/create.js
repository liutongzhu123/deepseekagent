const store = require('../../utils/mock-store');
const orderApi = require('../../utils/order-api');

Page({
  data: {
    profile: null,
    templates: ['演示委托模板 A', '演示委托模板 B'],
    templateName: '',
    project: '',
    standard: '',
    sampleName: '',
    quantity: '',
    notes: '',
    apiEnabled: false,
    submitting: false,
    orderId: '',
    orderError: '',
    pendingRecovery: false,
    pendingBlocked: false,
    pendingUnreadable: false,
    pendingClearedNeedsCheck: false,
  },
  async onLoad(options) {
    let profile = store.getProfile();
    try {
      const capability = wx.getStorageSync('demo_registration_capability_v1');
      const registration = capability && capability.registration;
      if (registration) profile = {
        companyName: registration.company_name,
        contactName: registration.contact_name,
        region: registration.region,
      };
    } catch (error) { /* The offline profile remains available. */ }
    const apiEnabled = orderApi.configured();
    this.setData({ profile, apiEnabled });
    if (apiEnabled && !profile) this.setData({ orderError: '请先完成本地演示登记并保存登记凭证' });
    if (apiEnabled && !(options && options.orderId)) {
      const pending = orderApi.readPending();
      if (pending && pending.blocked) {
        this.setData({ pendingBlocked: true, pendingUnreadable: Boolean(pending.unreadable), pendingClearedNeedsCheck: Boolean(pending.needsCheck), orderError: pending.needsCheck ? '本机委托重试编号已清理；请先核对服务端状态并确认继续' : (pending.unreadable ? '本机委托待办不可读取；为避免重复提交，已停止操作' : '此设备上有其他登记账号尚未确认的委托提交；请先切回原登记账号恢复处理') });
      } else if (pending && pending.payload) {
        this.setData({
          templateName: pending.payload.template_id,
          project: pending.payload.project_name,
          standard: pending.payload.standard,
          sampleName: pending.payload.sample_name,
          quantity: pending.payload.quantity,
          notes: pending.payload.notes,
          pendingRecovery: true,
        });
      }
    }
    if (options && options.orderId && apiEnabled) {
      try {
        const order = await orderApi.get(options.orderId);
        const pending = orderApi.readPending(options.orderId);
        if (pending && pending.blocked) {
          this.setData({ pendingBlocked: true, pendingUnreadable: Boolean(pending.unreadable), pendingClearedNeedsCheck: Boolean(pending.needsCheck), orderError: pending.needsCheck ? '本机委托重试编号已清理；请先核对服务端状态并确认继续' : (pending.unreadable ? '本机委托待办不可读取；为避免重复补件，已停止操作' : '设备上有另一登记账号或委托的补件结果尚未确认，请先切回原账号和委托恢复处理') });
          return;
        }
        const fields = pending && pending.payload ? pending.payload : order;
        const snapshot = order.template_snapshot || {};
        this.setData({
          orderId: order.order_id,
          templateName: pending && pending.payload ? fields.template_id : snapshot.label || '',
          project: fields.project_name || '',
          standard: fields.standard || '',
          sampleName: fields.sample_name || '',
          quantity: fields.quantity || '',
          notes: fields.notes || '',
          pendingRecovery: Boolean(pending && pending.payload),
        });
      } catch (error) { this.setData({ orderError: error.message || '委托读取失败' }); }
    }
  },
  onInput(event) { this.setData({ [event.currentTarget.dataset.field]: event.detail.value }); },
  chooseTemplate(event) { this.setData({ templateName: event.currentTarget.dataset.value }); },
  register() { wx.switchTab({ url: '/pages/profile/index' }); },
  clearPending() {
    if (this.data.pendingClearedNeedsCheck) {
      if (!orderApi.acknowledgeCleanupCheck()) {
        wx.showToast({ title: '无法保存核对确认，请重试', icon: 'none' });
        return;
      }
      const pending = orderApi.readPending(this.data.orderId);
      if (pending && pending.blocked) {
        this.setData({ pendingBlocked: true, pendingUnreadable: Boolean(pending.unreadable), pendingClearedNeedsCheck: Boolean(pending.needsCheck), orderError: pending.needsCheck ? '本机委托重试编号已清理；请先核对服务端状态并确认继续' : '本机委托待办仍不可恢复，请核对服务端状态' });
      } else if (pending && pending.payload) {
        this.setData({
          templateName: pending.payload.template_id,
          project: pending.payload.project_name,
          standard: pending.payload.standard,
          sampleName: pending.payload.sample_name,
          quantity: pending.payload.quantity,
          notes: pending.payload.notes,
          pendingBlocked: false, pendingClearedNeedsCheck: false, pendingRecovery: true, orderError: '',
        });
      } else this.setData({ pendingBlocked: false, pendingClearedNeedsCheck: false, orderError: '' });
      return;
    }
    wx.showModal({
      title: '清理本机委托待办？',
      content: '这会删除本机保存的委托重试编号。若服务端已经收到请求，服务器记录不会撤销；清理后请先刷新委托列表或联系管理员核对，再创建或补件。',
      success: (result) => {
        if (!result.confirm) return;
        const cleared = orderApi.forgetPending();
        if (cleared) this.setData({ pendingBlocked: false, pendingUnreadable: false, pendingClearedNeedsCheck: true, orderError: '本机重试编号已清理。请先核对服务端委托记录；确认后再继续创建或补件。', pendingRecovery: false });
        wx.showToast({ title: cleared ? '已清理本机待办' : '清理失败，请稍后重试', icon: cleared ? 'success' : 'none' });
      },
    });
  },
  async submit() {
    if (this.data.pendingClearedNeedsCheck) {
      wx.showToast({ title: '请先核对服务端委托状态并确认继续', icon: 'none' });
      return;
    }
    if (this.data.submitting) return;
    if (!this.data.templateName || !this.data.project.trim() || !this.data.sampleName.trim() || !this.data.quantity.trim()) {
      wx.showToast({ title: '请补全模板、项目、样品和数量', icon: 'none' });
      return;
    }
    wx.showModal({
      title: '确认演示委托单',
      content: this.data.apiEnabled
        ? `将为 ${this.data.profile.companyName} 提交一条本地服务端演示委托，需已批准的登记凭证。`
        : `将为 ${this.data.profile.companyName} 保存一条仅在本机的演示委托单。`,
      success: async (result) => {
        if (!result.confirm) return;
        this.setData({ submitting: true });
        try {
          if (this.data.apiEnabled) {
            const payload = {
              template_id: this.data.templateName,
              project_name: this.data.project,
              standard: this.data.standard,
              sample_name: this.data.sampleName,
              quantity: this.data.quantity,
              notes: this.data.notes,
            };
            if (this.data.orderId) await orderApi.update(this.data.orderId, payload);
            else await orderApi.submit(payload);
          } else {
            const order = store.addOrder(this.data);
            if (!order) throw new Error('本机保存失败，请检查资料或本机空间');
          }
          wx.showToast({ title: this.data.orderId ? '补充信息已提交' : (this.data.apiEnabled ? '演示委托已提交' : '演示委托已保存'), icon: 'success' });
          setTimeout(() => wx.navigateBack(), 500);
        } catch (error) {
          if (this.data.apiEnabled) {
            const pending = orderApi.readPending(this.data.orderId);
            this.setData({ pendingRecovery: Boolean(pending && pending.payload) });
          }
          wx.showToast({ title: error.message || '委托提交失败', icon: 'none' });
        } finally {
          this.setData({ submitting: false });
        }
      },
    });
  },
});
