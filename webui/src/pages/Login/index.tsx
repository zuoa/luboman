import services from '@/services/luboman';
import { LockOutlined } from '@ant-design/icons';
import { history, useModel } from '@umijs/max';
import { Button, Card, Form, Input, message } from 'antd';
import { useEffect, useState } from 'react';
import logoUrl from '@/assets/logo.svg';
import styles from './index.less';

const { login, getStatus } = services.Auth;

const LoginPage: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const { refresh } = useModel('@@initialState');

  // 未启用访问密码 / 已登录时直接进首页
  useEffect(() => {
    getStatus({ skipErrorHandler: true })
      .then((status) => {
        if (!status?.enabled || status?.logged_in) {
          history.replace('/home');
        }
      })
      .catch(() => {
        // 状态查询失败时停留登录页，登录请求会再暴露真实错误
      });
  }, []);

  const onFinish = async ({ password }: { password: string }) => {
    setLoading(true);
    try {
      await login(password, { skipErrorHandler: true });
      message.success('登录成功');
      refresh();
      history.replace('/home');
    } catch (error: any) {
      // 401 密码错误 / 429 触发锁定，message 由后端给出（含剩余锁定时长）
      message.error(error?.data?.message || error?.message || '登录失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={styles.container}>
      <Card className={styles.card}>
        <div className={styles.header}>
          <img src={logoUrl} alt="录播Man" className={styles.logo} />
          <div className={styles.title}>录播Man</div>
        </div>
        <Form onFinish={onFinish} size="large">
          <Form.Item
            name="password"
            rules={[{ required: true, message: '请输入访问密码' }]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="访问密码"
              autoFocus
            />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" block loading={loading}>
              登录
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
};

export default LoginPage;
