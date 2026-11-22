const store = require('../../utils/mock-store');

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
  },
  onLoad() { this.setData({ profile: store.getProfile() }); },
  onInput(event) { this.setData({ [event.currentTarget.dataset.field]: event.detail.value }); },
  chooseTemplate(event) { this.setData({ templateName: event.currentTarget.dataset.value }); },
  register() { wx.switchTab({ url: '/pages/profile/index' }); },
  submit() {
    if (!this.data.templateName || !this.data.project.trim() || !this.data.sampleName.trim() || !this.data.quantity.trim()) {
      wx.showToast({ title: '请补全模板、项目、样品和数量', icon: 'none' });
      return;
    }
    wx.showModal({
      title: '确认模拟委托单',
      content: `将为 ${this.data.profile.companyName} 保存一条本机演示委托单，状态为待模拟业务审核。`,
      success: (result) => {
        if (!result.confirm) return;
        const order = store.addOrder(this.data);
        if (!order) {
          wx.showToast({ title: '保存失败，请检查资料或本机空间', icon: 'none' });
          return;
        }
        wx.showToast({ title: '演示委托单已保存', icon: 'success' });
        setTimeout(() => wx.navigateBack(), 500);
      },
    });
  },
});
