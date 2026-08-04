import services from '@/services/luboman';
import { LogoutOutlined } from '@ant-design/icons';
import { useModel } from '@umijs/max';
import { Button, Popconfirm, Tooltip } from 'antd';

/** 顶栏退出登录按钮：仅在启用了访问密码（WEBUI_PASSWORD）时显示 */
const LogoutButton: React.FC = () => {
  const { initialState } = useModel('@@initialState');
  if (!initialState?.authEnabled) {
    return null;
  }

  const onLogout = async () => {
    try {
      await services.Auth.logout({ skipErrorHandler: true });
    } catch (error) {
      // 即使接口失败也跳登录页（cookie 过期场景）
    }
    window.location.href = '/login';
  };

  return (
    <Popconfirm title="确定退出登录？" onConfirm={onLogout} okText="退出" cancelText="取消">
      <Tooltip title="退出登录">
        <Button type="text" icon={<LogoutOutlined />} />
      </Tooltip>
    </Popconfirm>
  );
};

export default LogoutButton;
