const store = require('./mock-store');
const serviceApi = require('./service-api');

async function submit(input) {
  if (serviceApi.configured()) {
    const context = store.quoteContext(input, 'service');
    const conversation = await serviceApi.send(context, '');
    return { destination: 'service', conversation };
  }
  const quote = store.addQuote(input);
  if (!quote) throw new Error('本机存储失败，请重试');
  return { destination: 'local', quote };
}

async function handoff(input) {
  const context = store.quoteContext(input, serviceApi.configured() ? 'service' : 'local');
  if (!context) throw new Error('请先填写检测项目');
  if (serviceApi.configured()) {
    const conversation = await serviceApi.send(context, '');
    return { destination: 'service', conversation };
  }
  const messages = store.addMessage(context);
  if (!messages) throw new Error('本机存储失败，请重试');
  return { destination: 'local', messages };
}

module.exports = { submit, handoff };
