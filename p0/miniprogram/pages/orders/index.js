const store = require('../../utils/mock-store');
const orderApi = require('../../utils/order-api');

Page({
  data: { quotes: [], orders: [], apiEnabled: false, orderError: '' },
  onShow() { this.refresh(); },
  async refresh() {
    const apiEnabled = orderApi.configured();
    this.setData({ apiEnabled, quotes: store.listQuotes(), orderError: '' });
    if (!apiEnabled) { this.setData({ orders: store.listOrders() }); return; }
    try {
      const rows = await orderApi.list();
      this.setData({ orders: rows.map((item) => ({
        orderNo: item.order_id,
        orderId: item.order_id,
        rawStatus: item.status,
        companyName: item.company_name,
        templateName: item.template_snapshot && item.template_snapshot.label || '本地演示模板',
        project: item.project_name,
        sampleName: item.sample_name,
        quantity: item.quantity,
        standard: item.standard,
        status: ({ submitted: '待演示审核', needs_info: '待补件', approved: '待客户确认（演示）', rejected: '已拒绝（演示）', customer_confirmed: '客户已确认（演示）', customer_declined: '客户暂不接受（演示）' })[item.status] || item.status,
        createdAt: item.created_at,
        serverStored: true,
      })) });
    } catch (error) {
      this.setData({ orders: [], orderError: error.message || '演示委托读取失败' });
    }
  },
  goQuote() { wx.navigateTo({ url: '/pages/quote/index' }); },
  createOrder() {
    if (!store.getProfile() && !orderApi.configured()) {
      wx.showModal({
        title: '先登记演示客户资料',
        content: '委托单将从本机演示资料带入企业名称和联系人。',
        success: (result) => {
          if (result.confirm) wx.switchTab({ url: '/pages/profile/index' });
        },
      });
      return;
    }
    wx.navigateTo({ url: '/pages/orders/create' });
  },
  editOrder(event) {
    const orderId = event.currentTarget.dataset.orderId;
    if (orderId) wx.navigateTo({ url: `/pages/orders/create?orderId=${encodeURIComponent(orderId)}` });
  },
  confirmOrder(event) {
    const { orderId, decision } = event.currentTarget.dataset;
    const isAccept = decision === 'accepted';
    wx.showModal({
      title: isAccept ? '确认演示委托？' : '暂不接受演示委托？',
      content: '此操作只记录本地演示状态，不发送邮件、不创建检测任务，也不代表真实委托生效。',
      confirmText: isAccept ? '确认' : '暂不接受',
      success: async (result) => {
        if (!result.confirm) return;
        try {
          await orderApi.confirm(orderId, decision);
          wx.showToast({ title: isAccept ? '已记录演示确认' : '已记录暂不接受', icon: 'success' });
          this.refresh();
        } catch (error) { wx.showToast({ title: error.message || '确认结果未能确认，请刷新记录', icon: 'none', duration: 3000 }); }
      },
    });
  },
  clear() {
    wx.showModal({
      title: '清空本机演示数据？',
      content: '此操作会删除本小程序保存的客户资料、模拟需求、委托单和客服消息。',
      success: (result) => {
        if (!result.confirm) return;
        const cleared = store.clearDemoData();
        this.refresh();
        wx.showToast({ title: cleared ? '已清除' : '部分数据未清除', icon: cleared ? 'success' : 'none' });
      },
    });
  },
});
