App({
  globalData: {
    demoOnly: true,
    customerServiceDemoApiBase: '',
  },
  onLaunch() {
    // Configure the opt-in local demo endpoint through WeChat DevTools' extConfig.
    // An empty value intentionally disables network registration; never fall back
    // to local storage after a failed API request.
    try {
      const config = wx.getExtConfigSync ? wx.getExtConfigSync() : {};
      this.globalData.customerServiceDemoApiBase = String(
        config.customerServiceDemoApiBase || '',
      ).trim().replace(/\/$/, '');
    } catch (error) {
      this.globalData.customerServiceDemoApiBase = '';
    }
  },
});
