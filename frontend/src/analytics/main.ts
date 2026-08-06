import 'element-plus/dist/index.css'
import '../styles/base.css'
import './analytics.css'
import AnalyticsApp from './AnalyticsApp.vue'
import { mountProtectedPage } from '../shared/bootstrap'

mountProtectedPage(AnalyticsApp, 'query')
