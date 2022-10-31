#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (C) 2019 Istituto Italiano di Tecnologia (IIT)
# This software may be modified and distributed under the terms of the
# LGPL-2.1+ license. See the accompanying LICENSE file for details.

"""
This file contains the code of the benchmark service client.
Its features are:

1. connect to the ros camera topics to read rgb, depth, point cloud, camera parameters
2. send a request to a grasp planning algorithm of type GraspPlanner.srv
3. connect with robot to send cartesian grasp pose commands
(4. assess if the grasp was successful or not)
"""

import rospy
import warnings
import message_filters
import tf2_ros
import sensor_msgs.point_cloud2
from sensor_msgs.msg import Image, PointCloud2, CameraInfo
from sensor_msgs.point_cloud2 import read_points
from geometry_msgs.msg import Transform, Pose, PoseStamped, TransformStamped
from std_msgs.msg import Bool
from typing import List

from grasping_benchmarks.base.transformations import quaternion_to_matrix, matrix_to_quaternion

from grasping_benchmarks_ros.srv import UserCmd, UserCmdRequest, UserCmdResponse
from grasping_benchmarks_ros.srv import GraspPlanner, GraspPlannerRequest, GraspPlannerResponse
from grasping_benchmarks_ros.msg import BenchmarkGrasp

from panda_ros_common.srv import PandaGrasp, PandaGraspRequest, PandaGraspResponse

import numpy as np
import re

MAX_NUMBER_OF_CANDIDATES = 10

NEW_MSG = {
"new_data": False,
"data": {},
}

from shape_completion import complete_point_cloud
# import sys
# sys.path.append('/workspace/sources/shape_completion_network')
# from shape_completion_lundell import *


class GraspingBenchmarksManager(object):
    def __init__(self, grasp_planner_service_name, grasp_planner_service, user_cmd_service_name, panda_service_name, use_aruco, verbose=False):

        self._verbose = verbose
        self._use_aruco = use_aruco

        # --- new grasp command service --- #
        self._new_grasp_srv = rospy.Service(user_cmd_service_name, UserCmd, self.user_cmd)

        rospy.loginfo("GraspingBenchmarksManager: Waiting for grasp planner service...")
        rospy.wait_for_service(grasp_planner_service_name, timeout=30.0)
        self._grasp_planner = rospy.ServiceProxy(grasp_planner_service_name, GraspPlanner)
        rospy.loginfo("...Connected with service {}".format(grasp_planner_service_name))

        # --- panda service --- #
        panda_service_name =  "/panda_grasp_server/panda_grasp"
        rospy.loginfo("GraspingBenchmarksManager: Waiting for panda control service...")
        rospy.wait_for_service(panda_service_name, timeout=60.0)
        self._panda = rospy.ServiceProxy(panda_service_name, PandaGrasp)
        rospy.loginfo("...Connected with service {}".format(panda_service_name))

        # --- subscribers to camera topics --- #
        self._cam_info_sub = message_filters.Subscriber('/camera/aligned_depth_to_color/camera_info', CameraInfo)
        self._rgb_sub = message_filters.Subscriber('/camera/color/image_raw', Image)
        self._depth_sub = message_filters.Subscriber('/camera/aligned_depth_to_color/image_raw', Image)
        try:
            rospy.wait_for_message('/objects_cloud', PointCloud2, 1)
            self._pc_sub = message_filters.Subscriber('/objects_cloud', PointCloud2)
            rospy.wait_for_message('/plane_cloud', PointCloud2, 1)
            self._background_pc_sub = message_filters.Subscriber('/plane_cloud', PointCloud2)
        except rospy.exceptions.ROSException:
            self._pc_sub = message_filters.Subscriber('/camera/depth_registered/points', PointCloud2)
            # If there is no segmentation node, there is no foreground/background separation
            self._background_pc_sub = message_filters.Subscriber('/camera/depth_registered/points', PointCloud2)

        self._seg_sub = rospy.Subscriber('rgb/image_seg', Image, self.seg_img_callback, queue_size=10)

        # --- camera data synchronizer --- #
        self._tss = message_filters.ApproximateTimeSynchronizer([self._cam_info_sub, self._rgb_sub, self._depth_sub, self._pc_sub, self._background_pc_sub],
                                                                queue_size=1, slop=0.5)
        self._tss.registerCallback(self._camera_data_callback)

        # --- robot/camera transform listener --- #
        self._tfBuffer = tf2_ros.buffer.Buffer()
        listener = tf2_ros.transform_listener.TransformListener(self._tfBuffer)

        # --- camera messages --- #
        self._cam_info_msg = None
        self._rgb_msg = None
        self._depth_msg = None
        self._pc_msg = None
        self._background_pc_msg = None
        self._camera_pose = TransformStamped()
        self._root_reference_frame = 'panda_link0'
        if self._use_aruco:
            self._aruco_board_pose = TransformStamped()
            self._aruco_reference_frame = 'aruco_board'
        self._enable_grasp_filter = False

        self._seg_msg = NEW_MSG

        self._new_camera_data = False
        self._abort = False

    # ---------------------- #
    # Grasp planning handler #
    # ---------------------- #
    def user_cmd(self, req):
        """New grasp request handler

        Parameters
        ---------
        req: :obj:`ROS ServiceRequest`
            ROS `ServiceRequest` for grasp planner service.
        """
        if self._verbose:
            print("Received new command from user...")

        available_commands_dict = {}
        available_commands_dict['help'] = "display available commands"
        available_commands_dict['grasp'] = "compute a new grasp and send it to the robot for execution"
        available_commands_dict['get_candidates [n]'] = "computes n candidates and saves them to file"
        available_commands_dict['abort'] = "interrupt grasp computation / do not send computed pose to the robot"

        available_commands_string = ''.join(["{}: {}\n".format(cmd, available_commands_dict[cmd]) for cmd in available_commands_dict.keys()])

        valid_command = False
        for cmd_string in available_commands_dict.keys():
            if cmd_string.split()[0] in req.cmd.data:
                valid_command = True
        if not valid_command:
            rospy.logerr("Invalid command")
            rospy.loginfo("Available commands are: {}".format(available_commands_string))
            return Bool(False)

        self._abort = False

        if req.cmd.data == "help":
            print("Available commands are:\n", available_commands_string)
            return Bool(True)

        elif req.cmd.data == "grasp" or (req.cmd.data.split()[0] == "get_candidates" and len(req.cmd.data.split()) == 2):

            # --- get images --- #
            if self._verbose:
                print("... waiting for images ...")

            count = 0
            while not self._new_camera_data and count < 1000:
                count += 1

            if count >= 1000:
                print("...no images received")
                return Bool(False)

            self._new_camera_data = False

            # Create the service request
            planner_req = GraspPlannerRequest()

            # Fill in the 2D camera data
            planner_req.color_image = self._rgb_msg
            planner_req.depth_image = self._depth_msg
            planner_req.camera_info = self._cam_info_msg

            # Fill in the camera viewpoint field
            planner_req.view_point.header = self._camera_pose.header
            planner_req.view_point.pose.orientation = self._camera_pose.transform.rotation
            planner_req.view_point.pose.position = self._camera_pose.transform.translation

            # Transform the point cloud in the root reference frame and include it
            transform = self._camera_pose.transform
            pc_in = self._pc_msg
            background_pc_in = self._background_pc_msg
            tr_matrix = np.identity(4)
            tr_matrix[:3, :3] = quaternion_to_matrix([transform.rotation.x,
                                                    transform.rotation.y,
                                                    transform.rotation.z,
                                                    transform.rotation.w])
            tr_matrix[:3, 3] = [transform.translation.x,
                                transform.translation.y,
                                transform.translation.z]
            points=[]
            for p_in in read_points(pc_in, skip_nans=True, field_names=("x", "y", "z", "rgb")):
                p_transformed = np.dot(tr_matrix, np.array([p_in[0], p_in[1], p_in[2], 1.0]))
                p_out=[]
                p_out.append(p_transformed[0])
                p_out.append(p_transformed[1])
                p_out.append(p_transformed[2])
                p_out.append(p_in[3])
                points.append(p_out)

            # Completing shape (in camera reference frame)
            np_partial_pc = self.transform_pc_to_frame(np.asarray(points)[:,:3], tr_matrix)

            np_completed_pc = complete_point_cloud(np_partial_pc)
            # np_completed_pc = complete_point_cloud_lundell(np_partial_pc)
            np_completed_pc = self.transform_pc_to_frame(np_completed_pc, np.linalg.inv(tr_matrix))
            np_points_complete = np.ones((np_completed_pc.shape[0], 4))*points[0][3]
            np_points_complete[:,:3] = np_completed_pc

            points = []
            for point in np_points_complete:
                points.append(list(point))

            # If a background PC is present, include it in the completed output point cloud
            # Otherwise, the completed PC will not have any background in it
            if background_pc_in is not None:
                background_points = []
                for p_back in read_points(background_pc_in, skip_nans=True, field_names=("x", "y", "z", "rgb")):
                    p_tmp = []
                    p_tmp.append(p_back[0])
                    p_tmp.append(p_back[1])
                    p_tmp.append(p_back[2])
                    p_tmp.append(p_back[3])
                    background_points.append(p_tmp)
                background_points_arr = np.asarray(background_points)
                background_points_arr[:,:3] = self.transform_pc_to_frame(background_points_arr[:,:3], np.linalg.inv(tr_matrix))
                for point in background_points_arr:
                    points.append(list(point))

            header = pc_in.header
            header.frame_id = self._camera_pose.header.frame_id
            pc_out = sensor_msgs.point_cloud2.create_cloud(header=header, fields=pc_in.fields, points=points)

            planner_req.cloud = pc_out

            # Set number of candidates
            planner_req.n_of_candidates = MAX_NUMBER_OF_CANDIDATES if req.cmd.data == "grasp" else int(req.cmd.data.split()[1])

            # Fill in the arcuo board wrt world reference frame
            if self._use_aruco:
                planner_req.aruco_board.position = self._aruco_board_pose.transform.translation
                planner_req.aruco_board.orientation = self._aruco_board_pose.transform.rotation
                planner_req.grasp_filter_flag = self._enable_grasp_filter

            # Plan for grasps
            try:
                if self._verbose:
                    rospy.loginfo("Sending request to the grasping planner...")
                reply = self._grasp_planner(planner_req)
                if self._verbose:
                    rospy.loginfo("Received {} candidates..." .format(len(reply.grasp_candidates)))
            except rospy.ServiceException as e:
                print("Service {} call failed: {}" .format(self._grasp_planner.resolved_name, e))
                return Bool(False)

            # Handle abort command
            if self._abort:
                rospy.loginfo("Grasp execution was aborted by the user...")
                return Bool(False)

            # If we are required to grasp, test the feasibility of the candidates first
            # and send the best grasp between the feasible candidates
            if req.cmd.data == "grasp":
                feasible_candidates = self.get_feasible_grasps(reply.grasp_candidates)
                rospy.loginfo(f"Feasible candidates: {len(feasible_candidates)}/{len(reply.grasp_candidates)}")
                if not len(feasible_candidates):
                    if self._verbose:
                        rospy.logwarn("No feasible candidates...")
                    return Bool(False)
                else:
                    best_candidate = self.get_best_grasp(feasible_candidates)
                    if self._verbose:
                        rospy.loginfo("The following grasp is about to be executed:")
                        rospy.loginfo(best_candidate)
                        rospy.sleep(3)
                    return self.execute_grasp(best_candidate)
            else:
                return self.dump_grasps(reply.grasp_candidates)

        elif req.cmd.data == "abort":
            self._abort = True
            return Bool(True)

    def get_best_grasp(self, grasps:list) -> BenchmarkGrasp:
        """Returns the best grasp candidate in a list, according to its score

        Parameters
        ----------
        grasps : list[BenchmarkGrasp]

        Returns
        -------
        BenchmarkGrasp
            The best grasp according to the planner's own score
        """

        best_candidate = grasps[0]

        for grasp in grasps[1:]:
            if grasp.score.data > best_candidate.score.data:
                best_candidate = grasp

        return best_candidate

    def dump_grasps(self, grasps:list, dump_dir_base:str = "/workspace/dump_"):
        """Dumps a list of grasp candidates to textfiles. Will generate
        a grasp_candidates.txt, grasp_candidate_scores.txt, and a
        grasp_candidates_width.txt.

        Parameters
        ----------
        grasps : list[BenchmarkGrasp]
            Grasp candidates to dump

        dump_dir_base : str
            Base path where to dump files

        Returns
        -------
        Bool
            Always true
        """

        # Find a suitable dir to store candidates each time this function is called
        import os
        dump_dir_idx = 0
        while (os.path.exists(dump_dir_base+str(dump_dir_idx))):
            dump_dir_idx+=1
        dump_dir = dump_dir_base + str(dump_dir_idx)
        os.mkdir(dump_dir)
        poses_filename = os.path.join(dump_dir, 'grasp_candidates.txt')
        scores_filename = os.path.join(dump_dir,'grasp_candidates_scores.txt')
        width_filename = os.path.join(dump_dir,'grasp_candidates_width.txt')

        with open(poses_filename,'w') as poses_file, open(scores_filename,'w') as scores_file:
            # For each candidate, get the 4x4 affine matrix first
            for candidate in grasps:
                candidate_pose_orientation = candidate.pose.pose.orientation
                candidate_pose_position = candidate.pose.pose.position
                candidate_pose_score = candidate.score.data
                candidate_pose_affine = np.eye(4)
                candidate_pose_affine[:3, :3] = quaternion_to_matrix([candidate_pose_orientation.x,
                                                                      candidate_pose_orientation.y,
                                                                      candidate_pose_orientation.z,
                                                                      candidate_pose_orientation.w])
                candidate_pose_affine[:3, 3] = np.array([candidate_pose_position.x,
                                                        candidate_pose_position.y,
                                                        candidate_pose_position.z])
                for idx in range(candidate_pose_affine.shape[0]):
                    # Create weird format compatible with application
                    if idx != (candidate_pose_affine.shape[0]-1):
                        row_string = '[{}, {}, {}, {}],'.format(str(candidate_pose_affine[idx,0]),
                                                                str(candidate_pose_affine[idx,1]),
                                                                str(candidate_pose_affine[idx,2]),
                                                                str(candidate_pose_affine[idx,3]))
                    else:
                        row_string = '[{}, {}, {}, {}]\n'.format(str(candidate_pose_affine[idx,0]),
                                                                    str(candidate_pose_affine[idx,1]),
                                                                    str(candidate_pose_affine[idx,2]),
                                                                    str(candidate_pose_affine[idx,3]))
                    poses_file.write(row_string)

                score_string = '{}\n'.format(str(candidate_pose_score))
                scores_file.write(score_string)

        return Bool(True)

    def get_feasible_grasps(self, grasps:list) -> list:
        """Parse out the unfeasible grasp candidates from a list

        Parameters
        ----------
        grasps : list[BenchmarkGrasp]
           List of candidates to check

        Returns
        -------
        list[BenchmarkGrasp]
            List of feasible candidates according to the primitive server. Can be empty
        """

        feasible_candidates = []
        for index, candidate in enumerate(grasps):
            if self._verbose:
                rospy.loginfo("Testing candidate # {}..." .format(index))
            if self.test_grasp_feasibility(candidate):
                feasible_candidates.append(candidate)

        return feasible_candidates

    def test_grasp_feasibility(self, grasp:BenchmarkGrasp) -> bool:
        """Queries the primitive server to understand if a valid trajectory to
        the grasp exists (i.e. it is feasible on the real robot)

        Parameters
        ----------
        grasp : BenchmarkGrasp
            The grasp candidate

        Returns
        -------
        bool
            True if the candidate is feasible, false otherwise
        """

        grasp_request = PandaGraspRequest()
        grasp_request.grasp = grasp.pose
        grasp_request.width = grasp.width
        grasp_request.plan_only = True

        grasp_reply = self._panda(grasp_request)
        return grasp_reply.success

    def execute_grasp(self, grasp: BenchmarkGrasp):
        """Assumes the grasp pose is already in the root reference frame

        Parameters:
            - grasp (obj: BenchmarkGrasp msg)
        """

        panda_req = PandaGraspRequest()
        panda_req.grasp = grasp.pose
        panda_req.width = grasp.width

        if self._verbose:
            print("request to panda is: \n{}" .format(panda_req))

        try:
            reply = self._panda(panda_req)
            print("Service {} reply is: \n{}" .format(self._panda.resolved_name, reply))
            return Bool(True)

        except rospy.ServiceException as e:
            print("Service {} call failed: {}" .format(self._panda.resolved_name, e))
            return Bool(False)

    # ------------------- #
    # Camera data handler #
    # ------------------- #
    def _camera_data_callback(self, cam_info, rgb, depth, pc, background_pc):

        self._cam_info_msg = cam_info
        self._rgb_msg = rgb
        self._depth_msg = depth
        self._pc_msg = pc
        if pc != background_pc:
            self._background_pc_msg = background_pc
        else:
            self._background_pc_msg = None

        self._new_camera_data = True

        # Get the camera transform wrt the root reference frame of this class
        try:
            self._camera_pose = self._tfBuffer.lookup_transform(self._root_reference_frame, self._cam_info_msg.header.frame_id, rospy.Time())
        except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException):
            warnings.warn("tf listener could not get camera pose. Are you publishing camera poses on tf?")
            self._camera_pose = TransformStamped()
            self._camera_pose.transform.rotation.w = 1.0

        # Get the aruco board transform wrt the root reference frame of this class.
        # If the aruco board is not found an exception is thrown and   _enable_grasp_filter
        # is set false in order to avoid filtering in plan_grasp function in graspnet_grasp_planner.py
        if self._use_aruco:
            try:
                self._aruco_board_pose = self._tfBuffer.lookup_transform(self._root_reference_frame, self._aruco_reference_frame, rospy.Time(),rospy.Duration(1.0))
                self._enable_grasp_filter = True

            except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException):
                rospy.logwarn("tf listener could not get aruco board pose. Are you publishing aruco board poses on tf?")
                self._enable_grasp_filter = False


    def seg_img_callback(self, data):
        if self._verbose:
            print("Got segmentation image...")

        self._seg_msg['data'] = data
        self._seg_msg['new_data'] = True

    # ------------------- #
    # Camera data handler #
    # ------------------- #
    def transform_pc_to_frame(self, pc : np.ndarray, target : np.ndarray) -> np.ndarray:
        """Transform the point cloud to target reference frame

        Parameters
        ----------
        pc : np.ndarray
            nx3, float64 array of points
        target_frame : np.ndarray
            4x4 affine transformation

        Returns
        -------
        np.ndarray
            nx3, float64 array of points
        """

        # [F]_p         : point p in reference frame F
        # [F]_T_[f]     : frame f in reference frame F
        # r             : root ref frame
        # target        : target ref frame
        # p, pc         : point, point cloud (points as rows)
        # tr(r_pc) = r_T_cam * tr(cam_pc)
        # tr(cam_pc) = inv(r_T_cam) * tr(r_pc)

        r_pc = np.c_[pc, np.ones(pc.shape[0])]
        target_T_r = np.linalg.inv(target)
        target_pc = np.transpose(np.dot(target_T_r, np.transpose(r_pc)))

        return target_pc[:, :-1]


if __name__ == "__main__":
    # Init node
    rospy.init_node('grasping_benchmarks_manager')

    # Get rosparam config
    grasp_planner_service_name = rospy.get_param("~grasp_planner_service_name")
    grasp_planner_service = rospy.get_param("~grasp_planner_service")
    new_grasp_service_name = rospy.get_param("~user_cmd_service_name")
    panda_service_name = "panda_grasp" # rospy.get_param("/panda_service_name")
    use_aruco = rospy.get_param("~use_aruco")

    # Instantiate benchmark client class
    bench_manager = GraspingBenchmarksManager(grasp_planner_service_name, grasp_planner_service, new_grasp_service_name, panda_service_name, use_aruco, verbose=True)

    # Spin forever.
    rospy.spin()
