import { defineConfig } from '@umijs/max';
import { themeTokens } from './src/theme/tokens';

export default defineConfig({
  // dark:true 让 umi antd 插件自动注入 darkAlgorithm（见 .umi/plugin-antd/runtime.tsx）；
  // 切勿在此再传 theme.algorithm，否则会覆盖掉深色。令牌单一源见 src/theme/tokens.ts。
  antd: {
    dark: true,
    theme: themeTokens,
  },
  access: {},
  model: {},
  initialState: {},
  request: {},
  layout: {
    title: '录播Man',
  },
  proxy: {
    '/api': {
      target: process.env.LUBOMAN_BACKEND || 'http://127.0.0.1:5005',
      changeOrigin: true,
      pathRewrite: { '^/api': '' },
    },
  },
  routes: [
    {
      path: '/',
      redirect: '/home',
    },
    {
      name: '首页',
      path: '/home',
      component: './Home',
    },
    {
      name: '直播间管理',
      path: '/liveRoom',
      component: './LiveRoom',
    },
    {
      name: '录像文件',
      path: '/recordFile',
      component: './RecordFile',
    },
    {
      name: '切片任务',
      path: '/clipTask',
      component: './ClipTask',
    },
    {
      name: '录播设置',
      path: '/config',
      component: './Config',
    },
    {
      name: '投稿管理',
      path: '/bili',

      routes: [
        {
          name: '投稿任务',
          path: '/bili/tasks',
          component: './Bili/SubmissionTask',
        },
        {
          name: '投稿账号',
          path: '/bili/account',
          component: './Bili/BiliAccount',
        },
        {
          name: '投稿模版',
          path: '/bili/template',
          component: './Bili/BiliUploadTemplate',
        },
      ],
    },
  ],
  npmClient: 'npm',
});
