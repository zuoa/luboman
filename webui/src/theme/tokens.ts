import type { ThemeConfig } from 'antd';

/**
 * 录播Man 设计令牌 —— 单一事实源。
 *
 * - `themeTokens`：喂给 `.umirc.ts`（`antd.theme`）与 `src/app.ts` 的 `rootContainer`
 *   `ConfigProvider`，驱动所有 antd / Pro 组件的深色 + 现代配色。
 * - `palette`：底层色板原值。`src/global.less` 的 `:root` 变量须与本 palette 保持一致
 *   （LESS 无法 import TS，故手动镜像；改动时两处同步）。
 *
 * 主色 `#1668dc` 替代 antd-4 旧默认 `#1890ff`；表面色 L0–L3 构成克制现代深色层次。
 */
export const palette = {
  primary: '#1668dc',
  primaryHover: '#2f86e6',
  bgBase: '#0f0f0f', // L0 页面底
  bgContainer: '#1a1a1a', // L1 容器（卡片 / 表格体 / 弹窗体）
  bgElevated: '#222222', // L2 抬升（表头 / 下拉 / hover）
  bgSpotlight: '#2a2a2a', // L3 浮层（popover / tooltip）
  border: '#2a2a2a',
  borderSecondary: '#1f1f1f',
  text: 'rgba(255, 255, 255, 0.85)',
  textSecondary: 'rgba(255, 255, 255, 0.65)',
  textTertiary: 'rgba(255, 255, 255, 0.45)',
  success: '#52c41a',
  warning: '#faad14',
  error: '#ff4d4f',
} as const;

export const themeTokens: ThemeConfig = {
  token: {
    colorPrimary: palette.primary,
    colorInfo: palette.primary,
    colorSuccess: palette.success,
    colorWarning: palette.warning,
    colorError: palette.error,
    colorBgLayout: palette.bgBase,
    colorBgContainer: palette.bgContainer,
    colorBgElevated: palette.bgElevated,
    colorBgSpotlight: palette.bgSpotlight,
    colorBorder: palette.border,
    colorBorderSecondary: palette.borderSecondary,
    borderRadius: 8,
    borderRadiusLG: 12,
    fontSize: 14,
    controlHeight: 32,
    motionDurationMid: '0.25s',
    wireframe: false,
  },
  components: {
    Card: {
      headerBg: 'transparent',
      headerFontSize: 16,
      paddingLG: 24,
    },
    Table: {
      headerBg: palette.bgElevated,
      headerColor: palette.text,
      rowHoverBg: '#262626',
    },
    Menu: {
      itemSelectedBg: 'transparent',
      itemSelectedColor: '#ffffff',
      itemActiveBg: palette.bgElevated,
      itemHeight: 44,
    },
    Modal: {
      contentBg: palette.bgContainer,
      headerBg: palette.bgContainer,
    },
    Layout: {
      colorBgHeader: palette.bgElevated,
      colorBgSider: '#161616',
    },
  },
};
