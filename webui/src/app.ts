// 运行时配置
import { createElement, type ReactNode } from 'react';
import { RequestConfig } from '@umijs/max';
import { ConfigProvider, message as antdMessage, theme as antdTheme } from 'antd';
import { themeTokens } from '@/theme/tokens';
import SystemStatus from '@/components/SystemStatus';
import logoUrl from '@/assets/logo.svg';

// 全局初始化数据配置，用于 Layout 用户信息和权限初始化
export async function getInitialState(): Promise<{ name: string }> {
  return { name: '录播Man' };
}

export const layout = () => {
  return {
    logo: logoUrl,
    menu: {
      locale: false,
    },
    // 侧边菜单底部系统状态（app.ts 不能用 JSX，见 rootContainer 注释）
    menuFooterRender: (props: any) =>
      createElement(SystemStatus, { collapsed: props?.collapsed }),
  };
};

/**
 * 主题保险层：包一层 antd ConfigProvider，显式 darkAlgorithm + cssVar/hashed。
 * 注意：app.ts 会被 umi 的 es-module-lexer 扫描导出，该 lexer 不解析 JSX，
 * 故此处用 createElement 而非 JSX。与 .umirc.ts 的 antd.theme 共用同一 themeTokens，不冲突。
 * cssVar 若生效则 var(--ant-*) 可用；页面 LESS 不依赖它，统一用 global.less 的 --lb-*。
 */
export function rootContainer(container: ReactNode) {
  return createElement(
    ConfigProvider,
    {
      theme: {
        ...themeTokens,
        algorithm: [antdTheme.darkAlgorithm],
        cssVar: true,
        hashed: false,
      },
    },
    container,
  );
}

/**
 * 统一请求层
 * - responseInterceptors：仅当响应体含 `success` 键（后端统一包装）时解包出 data；
 *   业务失败（success===false）抛错，交由 errorHandler 统一处理。
 *   `/ping`（纯文本）/`/bili/archive/pre`（B站原始响应，无 success 键）不会被误解包。
 * - errorHandler：网络/业务错误统一弹一条 toast。
 */
export const request: RequestConfig = {
  timeout: 15000,
  responseInterceptors: [
    (response) => {
      const body = response.data;
      if (body && typeof body === 'object' && 'success' in body) {
        if (body.success) {
          response.data = body.data;
        } else {
          const err = new Error(body.message || '请求失败');
          (err as any).code = body.code;
          (err as any).data = body;
          throw err;
        }
      }
      return response;
    },
  ],
  errorConfig: {
    errorHandler(error: any, opts: any) {
      // 允许单个请求通过 { skipErrorHandler: true } 关闭统一 toast（自行降级处理）
      if (opts?.skipErrorHandler) {
        throw error;
      }
      const msg =
        error?.data?.message ||
        error?.message ||
        (error?.response?.status
          ? `请求错误 (${error.response.status})`
          : '网络异常，请重试');
      antdMessage.error(msg);
      throw error;
    },
  },
};
