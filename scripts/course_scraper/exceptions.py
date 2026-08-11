class ScraperError(Exception):
    """抓取模块的基类异常"""
    pass

class LoginTimeoutError(ScraperError):
    """用户登录超时异常"""
    pass

class CourseNotFoundError(ScraperError):
    """未能在页面中找到课程列表异常"""
    pass

class ContentExtractionError(ScraperError):
    """正文内容提取失败异常"""
    pass

class NetworkError(ScraperError):
    """网络请求或加载异常"""
    pass
